from __future__ import annotations

import asyncio
import sys
import time as _time
from typing import Optional
from urllib.parse import urlparse
from xml.etree import ElementTree

import aiohttp
import structlog

from tune_server.config import settings
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import DiscoveredDevice, OutputType

logger = structlog.get_logger()

MEDIA_RENDERER_URN = "urn:schemas-upnp-org:device:MediaRenderer:1"
SSDP_ALL = "ssdp:all"

# OpenHome / Linn search targets — these devices often don't respond to
# the standard MediaRenderer URN but *do* support DLNA AVTransport for
# basic play/pause/stop once discovered.
OPENHOME_SEARCH_TARGETS = [
    "urn:av-openhome-org:service:Product:1",
    "urn:av-openhome-org:service:Playlist:1",
    "urn:linn-co-uk:device:Source:1",
]

# URN prefixes that indicate an OpenHome / Linn device in SSDP responses
_OPENHOME_PREFIXES = (
    "urn:av-openhome-org:",
    "urn:linn-co-uk:",
)

# How many consecutive scan cycles a device can miss before being marked lost.
# Some devices (Denon DMP-A10, certain Marantz streamers) respond slowly or
# intermittently to M-SEARCH; a grace period prevents flapping.
# Increased to 3 because some renderers (Wiim, Marantz SR series) only
# respond to M-SEARCH every ~60s, which can span multiple 30s poll cycles.
_MISS_GRACE_CYCLES = 3

# Interval (in seconds) between periodic full re-discovery scans.
# Separate from the main 30s poll loop: every _PERIODIC_RESCAN_INTERVAL
# seconds we send a fresh M-SEARCH burst to catch devices that come
# online late or respond intermittently.
_PERIODIC_RESCAN_INTERVAL = 300  # 5 minutes


class SsdpDiscovery:
    """SSDP discovery for DLNA/UPnP Media Renderers."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._devices: dict[str, DiscoveredDevice] = {}
        self._dmr_devices: dict[str, object] = {}  # dev_id -> DmrDevice
        self._requester = None
        self._factory = None
        self._factory_non_strict = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._lock = asyncio.Lock()
        self._advertisement_listener = None
        # Per-USN failure counter: warn the first 3 times, then debug.
        # Avoids log spam from devices that exist on the network but never
        # respond to UPnP description fetch (e.g. powered-down audiophile gear).
        self._create_failures: dict[str, int] = {}
        # Consecutive scan misses per device — only mark lost after _MISS_GRACE_CYCLES
        self._miss_count: dict[str, int] = {}
        # Track whether the initial startup scan found any devices
        self._initial_scan_done = False
        # Monotonic timestamp of last full periodic rescan
        self._last_periodic_rescan: float = 0.0
        # Cache of previously seen device locations for unicast probing.
        # Maps device_id -> description URL (LOCATION from SSDP response).
        self._known_locations: dict[str, str] = {}

    def _log_create_failure(self, usn: str, location: str, name: str, exc: Exception) -> None:
        count = self._create_failures.get(usn, 0) + 1
        self._create_failures[usn] = count
        logger.debug(
            "ssdp_device_create_error",
            location=location, usn=usn, name=name,
            error=str(exc), failure_count=count,
        )

    @staticmethod
    async def _resolve_sonos_name(host: str, model_name: str) -> str | None:
        """Fetch the Sonos room name from its device description XML.

        Returns a friendly name like "Sonos Play:1 - Salon" or None on failure.
        """
        url = f"http://{host}:1400/xml/device_description.xml"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status != 200:
                        return None
                    text = await resp.text()
            root = ElementTree.fromstring(text)
            # Namespace-agnostic search for <roomName>
            for elem in root.iter():
                if elem.tag.endswith("}roomName") or elem.tag == "roomName":
                    room = (elem.text or "").strip()
                    if room:
                        short_model = model_name or "Speaker"
                        if short_model.lower().startswith("sonos "):
                            short_model = short_model[6:]
                        return f"Sonos {short_model} - {room}"
            return None
        except Exception as exc:
            logger.debug("sonos_room_name_fetch_failed", host=host, error=str(exc))
            return None

    async def _try_minimal_dmr(self, dev_id: str, location: str, name: str) -> bool:
        """Probe common control URLs with a MinimalDmrDevice as fallback."""
        from tune_server.outputs.minimal_dmr import MinimalDmrDevice

        parsed = urlparse(location)
        port = parsed.port or 80
        base_url = f"http://{parsed.hostname}:{port}"
        minimal = MinimalDmrDevice(name=name, base_url=base_url, description_url=location)

        try:
            if not await minimal.probe():
                return False
        except Exception as e:
            logger.debug("minimal_dmr_probe_error", name=name, error=str(e))
            return False

        resolved_name = minimal.name
        disc_device = DiscoveredDevice(
            id=dev_id,
            name=resolved_name,
            type=OutputType.DLNA,
            host=parsed.hostname or "",
            port=port,
            available=True,
            capabilities={
                "dlna": True,
                "model": "",
                "sink_protocols": [],
                "device_name": resolved_name,
                "minimal_dmr": True,
            },
        )
        async with self._lock:
            self._devices[dev_id] = disc_device
            self._dmr_devices[dev_id] = minimal

        await self._event_bus.emit(Event(
            type=EventType.DEVICE_DISCOVERED,
            data=disc_device.model_dump(),
            source="ssdp",
        ))
        logger.info("minimal_dmr_registered", name=resolved_name, host=parsed.hostname, port=port)
        return True

    @property
    def devices(self) -> dict[str, DiscoveredDevice]:
        return dict(self._devices)

    def get_dmr_device(self, device_id: str) -> Optional[object]:
        return self._dmr_devices.get(device_id)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._discovery_loop())
        # Start passive SSDP NOTIFY listener for real-time alive/byebye events
        # (drops detection latency from ~30s polling to <1s for devices that
        # properly advertise their lifecycle).
        try:
            from async_upnp_client.advertisement import SsdpAdvertisementListener

            self._advertisement_listener = SsdpAdvertisementListener(
                async_on_alive=self._on_ssdp_alive,
                async_on_byebye=self._on_ssdp_byebye,
            )
            await self._advertisement_listener.async_start()
            logger.info("ssdp_advertisement_listener_started")
        except ImportError:
            logger.debug("ssdp_advertisement_listener_unavailable")
        except Exception as e:
            logger.warning("ssdp_advertisement_listener_error", error=str(e))
            self._advertisement_listener = None
        logger.info("ssdp_discovery_started")

    async def stop(self) -> None:
        self._running = False
        if self._advertisement_listener:
            try:
                await self._advertisement_listener.async_stop()
            except Exception as e:
                logger.debug("ssdp_advertisement_stop_error", error=str(e))
            self._advertisement_listener = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.debug("ssdp_discovery_task_cancelled")
            self._task = None
        if self._requester:
            try:
                await self._requester.async_close_session()
            except Exception as e:
                logger.debug("ssdp_requester_close_error", error=str(e))

    @staticmethod
    def _is_relevant_urn(st: str) -> bool:
        """Check if an SSDP search target is a MediaRenderer, OpenHome, or root device URN.

        Accepting upnp:rootdevice catches slow-responding renderers (Denon DMP-A10,
        some Marantz streamers) that only advertise their root device, not the
        embedded MediaRenderer directly.
        """
        if MEDIA_RENDERER_URN in st:
            return True
        if st == "upnp:rootdevice":
            return True
        return any(st.startswith(p) for p in _OPENHOME_PREFIXES)

    async def _on_ssdp_alive(self, headers) -> None:
        """Real-time SSDP NOTIFY ssdp:alive — device is announcing itself."""
        usn = headers.get("usn", "") or ""
        st = headers.get("nt", "") or ""
        if not self._is_relevant_urn(st):
            return
        async with self._lock:
            # Only interesting if we haven't seen it yet, or it was marked lost
            existing = self._devices.get(usn)
            if existing and existing.available:
                return
        logger.info("ssdp_alive_received", usn=usn)
        # Let the next rescan do the heavy lifting (creating the UPnP device).
        # Trigger it now so we don't wait up to 30s for the polling cycle.
        try:
            asyncio.create_task(self.rescan())
        except Exception:
            pass

    async def _on_ssdp_byebye(self, headers) -> None:
        """Real-time SSDP NOTIFY ssdp:byebye — device is going offline."""
        usn = headers.get("usn", "") or ""
        st = headers.get("nt", "") or ""
        if not self._is_relevant_urn(st):
            return
        async with self._lock:
            device = self._devices.get(usn)
            if not device or not device.available:
                return
            device.available = False
        logger.info("ssdp_byebye_received", usn=usn, name=device.name)
        await self._event_bus.emit(Event(
            type=EventType.DEVICE_LOST,
            data={"id": usn, "name": device.name},
            source="ssdp",
        ))

    async def _discovery_loop(self) -> None:
        try:
            from async_upnp_client.aiohttp import AiohttpRequester
            from async_upnp_client.client_factory import UpnpFactory
            from async_upnp_client.search import async_search
            from async_upnp_client.profiles.dlna import DmrDevice

            self._requester = AiohttpRequester()
            self._factory = UpnpFactory(self._requester)
            # Non-strict factory tolerates vendor-specific XML namespaces
            # (e.g. Engineered SA / LHC renderers use a custom scpd namespace
            # instead of the standard urn:schemas-upnp-org:service-1-0).
            self._factory_non_strict = UpnpFactory(self._requester, non_strict=True)

            while self._running:
                try:
                    discovered = set()

                    async def _on_response(response) -> None:
                        usn = response.get("usn", "")
                        location = response.get("location", "")
                        st = response.get("st", "")

                        is_media_renderer = MEDIA_RENDERER_URN in st
                        is_openhome = any(st.startswith(p) for p in _OPENHOME_PREFIXES)
                        # Accept root-device and uuid responses from ssdp:all —
                        # these may contain an embedded MediaRenderer (Denon, Marantz).
                        is_root_or_uuid = (
                            st == "upnp:rootdevice"
                            or st.startswith("uuid:")
                        )

                        if not is_media_renderer and not is_openhome and not is_root_or_uuid:
                            return

                        if usn in discovered:
                            return
                        discovered.add(usn)

                        try:
                            # If already registered via a previous search target, skip
                            dev_id = usn or location
                            async with self._lock:
                                if dev_id in self._devices and self._devices[dev_id].available:
                                    return

                            if is_openhome and not is_media_renderer:
                                logger.info(
                                    "openhome_ssdp_response",
                                    usn=usn, st=st, location=location,
                                )

                            # Try strict parsing first; fall back to non-strict
                            # for devices with vendor-specific XML namespaces
                            try:
                                device = await self._factory.async_create_device(location)
                            except Exception:
                                device = await self._factory_non_strict.async_create_device(location)
                                logger.info(
                                    "ssdp_non_strict_device_created",
                                    location=location, name=device.friendly_name,
                                )

                            # For root-device / uuid responses (ssdp:all fallback),
                            # verify this device actually has a MediaRenderer before
                            # wrapping it as a DmrDevice.
                            if is_root_or_uuid and not is_media_renderer:
                                if not DmrDevice.is_profile_device(device):
                                    return  # not a renderer — skip silently

                            dmr = DmrDevice(device, event_handler=None)

                            name = device.friendly_name or "Unknown DLNA"
                            parsed = urlparse(device.device_url or location)

                            # Query sink protocol info for format detection
                            sink_protocols: list[str] = []
                            has_cm = False
                            try:
                                has_cm = dmr.has_get_protocol_info
                                if has_cm:
                                    await dmr.async_get_protocol_info()
                                    sink_protocols = dmr.sink_protocol_info or []
                                    if sink_protocols:
                                        dsd_entries = [p for p in sink_protocols if "dsd" in p.lower() or "dsf" in p.lower() or "dff" in p.lower()]
                                        logger.info("ssdp_sink_protocols", name=name, count=len(sink_protocols),
                                                    dsd_entries=len(dsd_entries), sample=sink_protocols[:5])
                                    else:
                                        logger.info("ssdp_sink_protocols_empty", name=name)
                                else:
                                    logger.info("ssdp_no_protocol_info", name=name)
                            except Exception as exc:
                                logger.warning("ssdp_protocol_info_error", name=name, error=str(exc))

                            manufacturer = (device.manufacturer or "").lower()
                            if "google" in manufacturer:
                                logger.debug("ssdp_skip_chromecast", name=name)
                                return

                            # Skip renderers hosted on this machine (same IP)
                            device_host = parsed.hostname or ""
                            local_ip = self._get_local_ip()
                            if device_host and local_ip and device_host in (local_ip, "127.0.0.1", "localhost"):
                                logger.debug("ssdp_skip_local_renderer", name=name, host=device_host)
                                return

                            # Resolve Sonos room name for friendlier display
                            if "sonos" in manufacturer or "sonos" in (device.model_name or "").lower():
                                sonos_name = await self._resolve_sonos_name(
                                    parsed.hostname or "", device.model_name or ""
                                )
                                if sonos_name:
                                    logger.info("sonos_room_resolved", raw=name, resolved=sonos_name)
                                    name = sonos_name

                            from tune_server.audio.formats import detect_max_channels_from_device_info, detect_max_channels_from_sink_protocols
                            proto_ch = detect_max_channels_from_sink_protocols(sink_protocols)
                            device_ch = detect_max_channels_from_device_info(name, device.model_name or "")
                            max_channels = max(proto_ch, device_ch or 2)

                            capabilities = {
                                "dlna": True,
                                "manufacturer": device.manufacturer or "",
                                "model": device.model_name or "",
                                "sink_protocols": sink_protocols,
                                "device_name": device.friendly_name or "",
                                "max_channels": max_channels,
                            }
                            if is_openhome:
                                capabilities["openhome"] = True

                            disc_device = DiscoveredDevice(
                                id=dev_id,
                                name=name,
                                type=OutputType.DLNA,
                                host=parsed.hostname or "",
                                port=parsed.port or 0,
                                available=True,
                                capabilities=capabilities,
                            )

                            self._create_failures.pop(usn, None)
                            # Remember this device's description URL for unicast probing
                            self._known_locations[dev_id] = location
                            async with self._lock:
                                was_lost = dev_id in self._devices and not self._devices[dev_id].available
                                is_new = dev_id not in self._devices
                                self._devices[dev_id] = disc_device
                                self._dmr_devices[dev_id] = dmr

                            if is_new or was_lost:
                                await self._event_bus.emit(Event(
                                    type=EventType.DEVICE_DISCOVERED,
                                    data=disc_device.model_dump(),
                                    source="ssdp",
                                ))
                                dsd_support = any("dsf" in p.lower() or "dsd" in p.lower() or "dff" in p.lower() for p in sink_protocols)
                                log_event = "openhome_dlna_device_found" if is_openhome else "dlna_device_found"
                                logger.info(
                                    log_event, name=name, model=device.model_name,
                                    id=dev_id, recovered=was_lost, dsd_native=dsd_support,
                                    sink_protocol_count=len(sink_protocols),
                                )

                        except Exception as exc:
                            _name = usn.split("uuid:")[-1].split("::")[0] if "uuid:" in usn else usn
                            fallback_id = usn or location

                            # Already have a working DMR from a previous probe — keep it alive
                            async with self._lock:
                                if fallback_id in self._dmr_devices:
                                    if fallback_id in self._devices:
                                        self._devices[fallback_id].available = True
                                    return

                            if is_openhome:
                                logger.info(
                                    "openhome_dmr_create_failed_trying_fallback",
                                    usn=usn, location=location, error=str(exc),
                                )

                            self._log_create_failure(usn, location, _name, exc)

                            # Try MinimalDmrDevice (probe SOAP directly, bypass XML)
                            if await self._try_minimal_dmr(fallback_id, location, _name or "Unknown DLNA"):
                                return

                            # Last resort: visible but not controllable
                            parsed_loc = urlparse(location)
                            fallback_name = _name or "Unknown DLNA"
                            fallback_caps: dict = {
                                "dlna": True,
                                "model": "",
                                "sink_protocols": [],
                                "device_name": fallback_name,
                                "xml_unavailable": True,
                                "description_url": location,
                            }
                            if is_openhome:
                                fallback_caps["openhome"] = True
                            fallback_device = DiscoveredDevice(
                                id=fallback_id,
                                name=fallback_name,
                                type=OutputType.DLNA,
                                host=parsed_loc.hostname or "",
                                port=parsed_loc.port or 0,
                                available=True,
                                capabilities=fallback_caps,
                            )
                            async with self._lock:
                                if fallback_id not in self._devices:
                                    self._devices[fallback_id] = fallback_device
                                    await self._event_bus.emit(Event(
                                        type=EventType.DEVICE_DISCOVERED,
                                        data=fallback_device.model_dump(),
                                        source="ssdp",
                                    ))
                                    logger.info(
                                        "dlna_device_fallback_registered",
                                        name=fallback_name, host=parsed_loc.hostname,
                                        openhome=is_openhome,
                                    )

                    # Try SSDP search — handle Windows multicast errors gracefully
                    async def _run_search(target: str, source_ip: str | None = None) -> None:
                        kwargs: dict = {"timeout": 10, "search_target": target}
                        if source_ip:
                            kwargs["source"] = source_ip
                        await async_search(_on_response, **kwargs)

                    _forced_source = self._get_local_ip()
                    try:
                        await _run_search(MEDIA_RENDERER_URN, _forced_source)
                    except (OSError, ValueError) as os_err:
                        logger.debug("ssdp_multicast_error", error=str(os_err))
                        try:
                            await _run_search(MEDIA_RENDERER_URN, None)
                        except Exception:
                            pass

                    # Windows multicast fix: also try binding to 0.0.0.0 since
                    # Windows multicast routing can be unpredictable with multiple
                    # NICs or VPN adapters. Only do this when we have a specific
                    # source IP (i.e. not already 0.0.0.0).
                    if sys.platform == "win32" and _forced_source and _forced_source != "0.0.0.0":
                        try:
                            await _run_search(MEDIA_RENDERER_URN, "0.0.0.0")
                        except OSError:
                            logger.debug("ssdp_windows_anyaddr_fallback_failed")
                        except Exception:
                            pass

                    # Search for OpenHome / Linn devices that may not respond
                    # to the standard MediaRenderer URN
                    for oh_target in OPENHOME_SEARCH_TARGETS:
                        try:
                            await _run_search(oh_target, _forced_source)
                        except OSError:
                            # Multicast may fail on some networks; try with explicit source
                            try:
                                source_ip = self._get_local_ip()
                                if source_ip:
                                    await _run_search(oh_target, source_ip)
                            except Exception:
                                pass
                        except Exception:
                            logger.debug("ssdp_openhome_search_error", target=oh_target)

                    # Broad ssdp:all fallback — catches devices (Denon DMP-A10,
                    # some Marantz streamers) that ignore the specific MediaRenderer
                    # URN but respond to the generic search target.  The callback
                    # filters and validates via DmrDevice.is_profile_device().
                    try:
                        await _run_search(SSDP_ALL, _forced_source)
                    except (OSError, ValueError):
                        try:
                            await _run_search(SSDP_ALL, None)
                        except Exception:
                            pass
                    except Exception:
                        logger.debug("ssdp_all_search_error")

                    # Mark lost devices (with grace period for slow responders).
                    # Skip if no devices were discovered at all (search failure).
                    if discovered:
                        # Phase 1: under the lock, update miss counts and collect
                        # candidates that exceeded the grace period for unicast probing.
                        probe_candidates: list[str] = []
                        async with self._lock:
                            for dev_id in list(self._devices.keys()):
                                if dev_id in discovered:
                                    self._miss_count.pop(dev_id, None)
                                else:
                                    device = self._devices[dev_id]
                                    if device.available:
                                        misses = self._miss_count.get(dev_id, 0) + 1
                                        self._miss_count[dev_id] = misses
                                        if misses >= _MISS_GRACE_CYCLES:
                                            probe_candidates.append(dev_id)

                        # Phase 2: unicast-probe candidates outside the lock
                        # (each probe makes a 3s HTTP request).
                        for dev_id in probe_candidates:
                            if await self._unicast_probe(dev_id):
                                self._miss_count.pop(dev_id, None)
                            else:
                                lost_name: str | None = None
                                async with self._lock:
                                    device = self._devices.get(dev_id)
                                    if device and device.available:
                                        device.available = False
                                        lost_name = device.name
                                        self._miss_count.pop(dev_id, None)
                                if lost_name is not None:
                                    await self._event_bus.emit(Event(
                                        type=EventType.DEVICE_LOST,
                                        data={"id": dev_id, "name": lost_name},
                                        source="ssdp",
                                    ))

                except Exception:
                    logger.exception("ssdp_scan_error")

                # Startup retry: if the first scan found nothing, schedule
                # a quick retry after 30s. Windows firewall or slow network
                # stacks may not respond to the very first multicast burst.
                if not self._initial_scan_done:
                    self._initial_scan_done = True
                    if not discovered:
                        logger.info("ssdp_startup_no_devices_scheduling_retry")
                        await asyncio.sleep(30)
                        continue  # immediately re-scan without waiting another 30s

                # Periodic full re-discovery every 5 minutes to catch devices
                # that come online late or respond intermittently.
                now = _time.monotonic()
                if now - self._last_periodic_rescan >= _PERIODIC_RESCAN_INTERVAL:
                    self._last_periodic_rescan = now
                    logger.info("ssdp_periodic_rescan", interval=_PERIODIC_RESCAN_INTERVAL)

                await asyncio.sleep(30)

        except ImportError as e:
            logger.warning("async_upnp_client_not_installed", error=str(e))
        except asyncio.CancelledError:
            raise

    async def rescan(self) -> list[DiscoveredDevice]:
        """Run a single SSDP scan immediately and return discovered devices."""
        try:
            from async_upnp_client.aiohttp import AiohttpRequester
            from async_upnp_client.client_factory import UpnpFactory
            from async_upnp_client.search import async_search
            from async_upnp_client.profiles.dlna import DmrDevice
        except ImportError:
            logger.debug("async_upnp_client_not_available_for_rescan")
            return []

        if not self._requester:
            self._requester = AiohttpRequester()
            self._factory = UpnpFactory(self._requester)
            self._factory_non_strict = UpnpFactory(self._requester, non_strict=True)

        discovered = set()

        async def _on_response(response) -> None:
            usn = response.get("usn", "")
            location = response.get("location", "")
            st = response.get("st", "")

            is_media_renderer = MEDIA_RENDERER_URN in st
            is_openhome = any(st.startswith(p) for p in _OPENHOME_PREFIXES)
            is_root_or_uuid = (
                st == "upnp:rootdevice"
                or st.startswith("uuid:")
            )

            if not is_media_renderer and not is_openhome and not is_root_or_uuid:
                return
            if usn in discovered:
                return
            discovered.add(usn)

            try:
                dev_id = usn or location
                async with self._lock:
                    if dev_id in self._devices and self._devices[dev_id].available:
                        return

                if is_openhome and not is_media_renderer:
                    logger.info("openhome_ssdp_response", usn=usn, st=st, location=location)

                # Try strict parsing first; fall back to non-strict
                # for devices with vendor-specific XML namespaces
                try:
                    device = await self._factory.async_create_device(location)
                except Exception:
                    device = await self._factory_non_strict.async_create_device(location)
                    logger.info(
                        "ssdp_non_strict_device_created",
                        location=location, name=device.friendly_name,
                    )

                # For root-device / uuid responses, verify MediaRenderer capability
                if is_root_or_uuid and not is_media_renderer:
                    if not DmrDevice.is_profile_device(device):
                        return

                dmr = DmrDevice(device, event_handler=None)

                name = device.friendly_name or "Unknown DLNA"
                parsed = urlparse(device.device_url or location)

                sink_protocols: list[str] = []
                try:
                    if dmr.has_get_protocol_info:
                        await dmr.async_get_protocol_info()
                        sink_protocols = dmr.sink_protocol_info or []
                except Exception as e:
                    logger.debug("ssdp_protocol_info_fetch_error", name=name, error=str(e))

                manufacturer = (device.manufacturer or "").lower()
                if "google" in manufacturer:
                    logger.debug("ssdp_skip_chromecast_rescan", name=name)
                    return

                # Resolve Sonos room name for friendlier display
                if "sonos" in manufacturer or "sonos" in (device.model_name or "").lower():
                    sonos_name = await self._resolve_sonos_name(
                        parsed.hostname or "", device.model_name or ""
                    )
                    if sonos_name:
                        logger.info("sonos_room_resolved", raw=name, resolved=sonos_name)
                        name = sonos_name

                from tune_server.audio.formats import detect_max_channels_from_device_info, detect_max_channels_from_sink_protocols
                proto_ch = detect_max_channels_from_sink_protocols(sink_protocols)
                device_ch = detect_max_channels_from_device_info(name, device.model_name or "")
                max_channels = max(proto_ch, device_ch or 2)

                capabilities = {
                    "dlna": True,
                    "manufacturer": device.manufacturer or "",
                    "model": device.model_name or "",
                    "sink_protocols": sink_protocols,
                    "device_name": device.friendly_name or "",
                    "max_channels": max_channels,
                }
                if is_openhome:
                    capabilities["openhome"] = True

                disc_device = DiscoveredDevice(
                    id=dev_id,
                    name=name,
                    type=OutputType.DLNA,
                    host=parsed.hostname or "",
                    port=parsed.port or 0,
                    available=True,
                    capabilities=capabilities,
                )

                self._create_failures.pop(usn, None)
                # Remember this device's description URL for unicast probing
                self._known_locations[dev_id] = location
                async with self._lock:
                    was_lost = dev_id in self._devices and not self._devices[dev_id].available
                    is_new = dev_id not in self._devices
                    self._devices[dev_id] = disc_device
                    self._dmr_devices[dev_id] = dmr

                if is_new or was_lost:
                    await self._event_bus.emit(Event(
                        type=EventType.DEVICE_DISCOVERED,
                        data=disc_device.model_dump(),
                        source="ssdp",
                    ))
                    log_event = "openhome_dlna_device_found" if is_openhome else "dlna_device_found"
                    logger.info(log_event, name=name, id=dev_id, recovered=was_lost)

            except Exception as exc:
                _name = usn.split("uuid:")[-1].split("::")[0] if "uuid:" in usn else usn
                fallback_id = usn or location

                async with self._lock:
                    if fallback_id in self._dmr_devices:
                        if fallback_id in self._devices:
                            self._devices[fallback_id].available = True
                        return

                self._log_create_failure(usn, location, _name, exc)

                if await self._try_minimal_dmr(fallback_id, location, _name or "Unknown DLNA"):
                    return

                parsed_loc = urlparse(location)
                fallback_name = _name or "Unknown DLNA"
                fallback_caps: dict = {
                    "dlna": True, "model": "", "sink_protocols": [],
                    "device_name": fallback_name, "xml_unavailable": True,
                    "description_url": location,
                }
                if is_openhome:
                    fallback_caps["openhome"] = True
                fallback_device = DiscoveredDevice(
                    id=fallback_id,
                    name=fallback_name,
                    type=OutputType.DLNA,
                    host=parsed_loc.hostname or "",
                    port=parsed_loc.port or 0,
                    available=True,
                    capabilities=fallback_caps,
                )
                async with self._lock:
                    if fallback_id not in self._devices:
                        self._devices[fallback_id] = fallback_device
                        logger.info(
                            "dlna_device_fallback_registered",
                            name=fallback_name, host=parsed_loc.hostname,
                            openhome=is_openhome,
                        )

        async def _run_search(target: str, source_ip: str | None = None) -> None:
            kwargs: dict = {"timeout": 10, "search_target": target}
            if source_ip:
                kwargs["source"] = source_ip
            await async_search(_on_response, **kwargs)

        try:
            await _run_search(MEDIA_RENDERER_URN)
        except (OSError, ValueError):
            try:
                source_ip = self._get_local_ip()
                if source_ip:
                    await _run_search(MEDIA_RENDERER_URN, source_ip)
            except (OSError, ValueError):
                pass

        # Search for OpenHome / Linn devices
        for oh_target in OPENHOME_SEARCH_TARGETS:
            try:
                await _run_search(oh_target)
            except OSError:
                try:
                    source_ip = self._get_local_ip()
                    if source_ip:
                        await _run_search(oh_target, source_ip)
                except Exception:
                    pass
            except Exception:
                logger.debug("ssdp_openhome_rescan_error", target=oh_target)

        # Broad ssdp:all fallback for slow-responding renderers
        try:
            await _run_search(SSDP_ALL)
        except OSError:
            try:
                source_ip = self._get_local_ip()
                if source_ip:
                    await _run_search(SSDP_ALL, source_ip)
            except Exception:
                pass
        except Exception:
            logger.debug("ssdp_all_rescan_error")

        return list(self._devices.values())

    @staticmethod
    def _get_local_ip() -> str | None:
        """Get the local IP address for binding SSDP multicast."""
        from tune_server.config import settings as _s
        if _s.advertise_ip:
            return _s.advertise_ip
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception as e:
            logger.debug("ssdp_local_ip_detection_failed", error=str(e))
            return None

    async def _unicast_probe(self, dev_id: str) -> bool:
        """Attempt a direct HTTP GET to a device's last known description URL.

        When a device was seen before but stopped responding to multicast
        M-SEARCH, it may still be reachable via unicast.  This avoids false
        "device lost" events for renderers that are slow to respond to
        multicast (Denon, Marantz, Wiim) or on networks where multicast
        is unreliable (Windows with multiple NICs / VPN).

        Returns True if the device is still alive (HTTP 200 on its
        description URL), False otherwise.
        """
        location = self._known_locations.get(dev_id)
        if not location:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    location,
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    if resp.status == 200:
                        logger.debug(
                            "ssdp_unicast_probe_alive",
                            dev_id=dev_id, location=location,
                        )
                        return True
        except Exception as exc:
            logger.debug(
                "ssdp_unicast_probe_failed",
                dev_id=dev_id, location=location, error=str(exc),
            )
        return False

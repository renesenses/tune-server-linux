from __future__ import annotations

import asyncio
from typing import Optional
from urllib.parse import urlparse

import structlog

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import DiscoveredDevice, OutputType

logger = structlog.get_logger()

MEDIA_RENDERER_URN = "urn:schemas-upnp-org:device:MediaRenderer:1"


class SsdpDiscovery:
    """SSDP discovery for DLNA/UPnP Media Renderers."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._devices: dict[str, DiscoveredDevice] = {}
        self._dmr_devices: dict[str, object] = {}  # dev_id -> DmrDevice
        self._requester = None
        self._factory = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._lock = asyncio.Lock()
        self._advertisement_listener = None
        # Per-USN failure counter: warn the first 3 times, then debug.
        # Avoids log spam from devices that exist on the network but never
        # respond to UPnP description fetch (e.g. powered-down audiophile gear).
        self._create_failures: dict[str, int] = {}

    def _log_create_failure(self, usn: str, location: str, name: str, exc: Exception) -> None:
        count = self._create_failures.get(usn, 0) + 1
        self._create_failures[usn] = count
        log = logger.warning if count <= 3 else logger.debug
        log(
            "ssdp_device_create_error",
            location=location, usn=usn, name=name,
            error=str(exc), failure_count=count,
        )

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

    async def _on_ssdp_alive(self, headers) -> None:
        """Real-time SSDP NOTIFY ssdp:alive — device is announcing itself."""
        usn = headers.get("usn", "") or ""
        st = headers.get("nt", "") or ""
        if MEDIA_RENDERER_URN not in st:
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
        if MEDIA_RENDERER_URN not in st:
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

            while self._running:
                try:
                    discovered = set()

                    async def _on_response(response) -> None:
                        usn = response.get("usn", "")
                        location = response.get("location", "")
                        st = response.get("st", "")

                        if MEDIA_RENDERER_URN not in st:
                            return

                        if usn in discovered:
                            return
                        discovered.add(usn)

                        try:
                            device = await self._factory.async_create_device(location)
                            dmr = DmrDevice(device, event_handler=None)

                            dev_id = usn or location
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
                                continue

                            disc_device = DiscoveredDevice(
                                id=dev_id,
                                name=name,
                                type=OutputType.DLNA,
                                host=parsed.hostname or "",
                                port=parsed.port or 0,
                                available=True,
                                capabilities={
                                    "dlna": True,
                                    "manufacturer": device.manufacturer or "",
                                    "model": device.model_name or "",
                                    "sink_protocols": sink_protocols,
                                    "device_name": device.friendly_name or "",
                                },
                            )

                            self._create_failures.pop(usn, None)
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
                                logger.info(
                                    "dlna_device_found", name=name, model=device.model_name,
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

                            self._log_create_failure(usn, location, _name, exc)

                            # Try MinimalDmrDevice (probe SOAP directly, bypass XML)
                            if await self._try_minimal_dmr(fallback_id, location, _name or "Unknown DLNA"):
                                return

                            # Last resort: visible but not controllable
                            parsed_loc = urlparse(location)
                            fallback_name = _name or "Unknown DLNA"
                            fallback_device = DiscoveredDevice(
                                id=fallback_id,
                                name=fallback_name,
                                type=OutputType.DLNA,
                                host=parsed_loc.hostname or "",
                                port=parsed_loc.port or 0,
                                available=True,
                                capabilities={
                                    "dlna": True,
                                    "model": "",
                                    "sink_protocols": [],
                                    "device_name": fallback_name,
                                    "xml_unavailable": True,
                                    "description_url": location,
                                },
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
                                    )

                    # Try SSDP search — handle Windows multicast errors gracefully
                    try:
                        await async_search(_on_response, timeout=10, search_target=MEDIA_RENDERER_URN)
                    except OSError as os_err:
                        # WinError 10065 (host unreachable) or similar — retry with source IP
                        logger.warning("ssdp_multicast_error", error=str(os_err))
                        try:
                            source_ip = self._get_local_ip()
                            if source_ip:
                                logger.info("ssdp_retry_with_source", source=source_ip)
                                await async_search(
                                    _on_response, timeout=10,
                                    search_target=MEDIA_RENDERER_URN,
                                    source=source_ip,
                                )
                        except Exception:
                            logger.debug("ssdp_retry_also_failed")

                    # Mark lost devices
                    async with self._lock:
                        for dev_id in list(self._devices.keys()):
                            if dev_id not in discovered:
                                device = self._devices[dev_id]
                                if device.available:
                                    device.available = False
                                    await self._event_bus.emit(Event(
                                        type=EventType.DEVICE_LOST,
                                        data={"id": dev_id, "name": device.name},
                                        source="ssdp",
                                    ))

                except Exception:
                    logger.exception("ssdp_scan_error")

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

        discovered = set()

        async def _on_response(response) -> None:
            usn = response.get("usn", "")
            location = response.get("location", "")
            st = response.get("st", "")

            if MEDIA_RENDERER_URN not in st or usn in discovered:
                return
            discovered.add(usn)

            try:
                device = await self._factory.async_create_device(location)
                dmr = DmrDevice(device, event_handler=None)

                dev_id = usn or location
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
                    continue

                disc_device = DiscoveredDevice(
                    id=dev_id,
                    name=name,
                    type=OutputType.DLNA,
                    host=parsed.hostname or "",
                    port=parsed.port or 0,
                    available=True,
                    capabilities={
                        "dlna": True,
                        "manufacturer": device.manufacturer or "",
                        "model": device.model_name or "",
                        "sink_protocols": sink_protocols,
                        "device_name": device.friendly_name or "",
                    },
                )

                self._create_failures.pop(usn, None)
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
                    logger.info("dlna_device_found", name=name, id=dev_id, recovered=was_lost)

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
                fallback_device = DiscoveredDevice(
                    id=fallback_id,
                    name=fallback_name,
                    type=OutputType.DLNA,
                    host=parsed_loc.hostname or "",
                    port=parsed_loc.port or 0,
                    available=True,
                    capabilities={
                        "dlna": True, "model": "", "sink_protocols": [],
                        "device_name": fallback_name, "xml_unavailable": True,
                        "description_url": location,
                    },
                )
                async with self._lock:
                    if fallback_id not in self._devices:
                        self._devices[fallback_id] = fallback_device
                        logger.info("dlna_device_fallback_registered", name=fallback_name, host=parsed_loc.hostname)

        try:
            await async_search(_on_response, timeout=10, search_target=MEDIA_RENDERER_URN)
        except OSError:
            source_ip = self._get_local_ip()
            if source_ip:
                await async_search(_on_response, timeout=10, search_target=MEDIA_RENDERER_URN, source=source_ip)
        return list(self._devices.values())

    @staticmethod
    def _get_local_ip() -> str | None:
        """Get the local IP address for binding SSDP multicast."""
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

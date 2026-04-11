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

    @property
    def devices(self) -> dict[str, DiscoveredDevice]:
        return dict(self._devices)

    def get_dmr_device(self, device_id: str) -> Optional[object]:
        return self._dmr_devices.get(device_id)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._discovery_loop())
        logger.info("ssdp_discovery_started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._requester:
            try:
                await self._requester.async_close_session()
            except Exception:
                pass

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
                                        logger.debug("ssdp_sink_protocols", name=name, count=len(sink_protocols),
                                                     sample=sink_protocols[:5])
                            except Exception:
                                logger.debug("ssdp_protocol_info_error", name=name)

                            disc_device = DiscoveredDevice(
                                id=dev_id,
                                name=name,
                                type=OutputType.DLNA,
                                host=parsed.hostname or "",
                                port=parsed.port or 0,
                                available=True,
                                capabilities={
                                    "dlna": True,
                                    "model": device.model_name or "",
                                    "sink_protocols": sink_protocols,
                                    "device_name": device.friendly_name or "",
                                },
                            )

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

                        except Exception:
                            logger.debug("ssdp_device_create_error", location=location)

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
                except Exception:
                    pass

                disc_device = DiscoveredDevice(
                    id=dev_id,
                    name=name,
                    type=OutputType.DLNA,
                    host=parsed.hostname or "",
                    port=parsed.port or 0,
                    available=True,
                    capabilities={
                        "dlna": True,
                        "model": device.model_name or "",
                        "sink_protocols": sink_protocols,
                        "device_name": device.friendly_name or "",
                    },
                )

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

            except Exception:
                logger.debug("ssdp_device_create_error", location=location)

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
        except Exception:
            return None

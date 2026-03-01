from __future__ import annotations

import asyncio
from typing import Optional
from urllib.parse import urlparse

import structlog

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import DiscoveredMediaServer

logger = structlog.get_logger()

MEDIA_SERVER_URN = "urn:schemas-upnp-org:device:MediaServer:1"


class MediaServerDiscovery:
    """SSDP discovery for DLNA/UPnP Media Servers (ContentDirectory)."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._servers: dict[str, DiscoveredMediaServer] = {}
        self._upnp_devices: dict[str, object] = {}  # server_id -> UpnpDevice
        self._requester = None
        self._factory = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._lock = asyncio.Lock()

    @property
    def servers(self) -> dict[str, DiscoveredMediaServer]:
        return dict(self._servers)

    def get_upnp_device(self, server_id: str) -> Optional[object]:
        return self._upnp_devices.get(server_id)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._discovery_loop())
        logger.info("media_server_discovery_started")

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

            self._requester = AiohttpRequester()
            self._factory = UpnpFactory(self._requester)

            while self._running:
                try:
                    discovered = set()

                    async def _on_response(response) -> None:
                        usn = response.get("usn", "")
                        location = response.get("location", "")
                        st = response.get("st", "")

                        if MEDIA_SERVER_URN not in st:
                            return

                        if usn in discovered:
                            return
                        discovered.add(usn)

                        try:
                            device = await self._factory.async_create_device(location)

                            dev_id = usn or location
                            name = device.friendly_name or "Unknown MediaServer"
                            parsed = urlparse(device.device_url or location)

                            server = DiscoveredMediaServer(
                                id=dev_id,
                                name=name,
                                host=parsed.hostname or "",
                                port=parsed.port or 0,
                                manufacturer=device.manufacturer or None,
                                model=device.model_name or None,
                                available=True,
                            )

                            async with self._lock:
                                was_lost = dev_id in self._servers and not self._servers[dev_id].available
                                is_new = dev_id not in self._servers
                                self._servers[dev_id] = server
                                self._upnp_devices[dev_id] = device

                            if is_new or was_lost:
                                await self._event_bus.emit(Event(
                                    type=EventType.MEDIA_SERVER_DISCOVERED,
                                    data=server.model_dump(),
                                    source="media_server_discovery",
                                ))
                                logger.info(
                                    "media_server_found",
                                    name=name,
                                    manufacturer=device.manufacturer,
                                    model=device.model_name,
                                    id=dev_id,
                                    recovered=was_lost,
                                )

                        except Exception:
                            logger.debug("media_server_create_error", location=location)

                    await async_search(_on_response, timeout=10, search_target=MEDIA_SERVER_URN)

                    # Mark lost servers
                    async with self._lock:
                        for dev_id in list(self._servers.keys()):
                            if dev_id not in discovered:
                                server = self._servers[dev_id]
                                if server.available:
                                    server.available = False
                                    await self._event_bus.emit(Event(
                                        type=EventType.MEDIA_SERVER_LOST,
                                        data={"id": dev_id, "name": server.name},
                                        source="media_server_discovery",
                                    ))

                except Exception:
                    logger.exception("media_server_scan_error")

                await asyncio.sleep(30)

        except ImportError as e:
            logger.warning("async_upnp_client_not_installed", error=str(e))
        except asyncio.CancelledError:
            raise

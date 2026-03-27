"""UPnP MediaServer — orchestrateur principal."""
from __future__ import annotations

import hashlib
import socket

import structlog
from aiohttp import web

from tune_server.event_bus import EventBus, EventType
from tune_server.upnp_server.audio_handler import register_audio_routes
from tune_server.upnp_server.connection_manager import register_cm_routes
from tune_server.upnp_server.content_directory import ContentDirectoryHandler, register_cd_routes
from tune_server.upnp_server.descriptors import register_descriptor_routes
from tune_server.upnp_server.ssdp_advertiser import SsdpAdvertiser

logger = structlog.get_logger()


def _generate_uuid() -> str:
    """Generate a stable UUID based on hostname."""
    hostname = socket.gethostname()
    h = hashlib.md5(hostname.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


class UpnpMediaServer:
    def __init__(
        self,
        server_ip: str,
        http_port: int,
        api_port: int,
        aiohttp_app: web.Application,
        track_repo,
        album_repo,
        artist_repo,
        event_bus: EventBus,
        friendly_name: str = "Tune Server",
    ) -> None:
        self._uuid = _generate_uuid()
        self._ip = server_ip
        self._http_port = http_port
        self._api_port = api_port
        self._app = aiohttp_app
        self._event_bus = event_bus
        self._friendly_name = friendly_name

        self._cd_handler = ContentDirectoryHandler(
            track_repo=track_repo,
            album_repo=album_repo,
            artist_repo=artist_repo,
            server_ip=server_ip,
            http_port=http_port,
            api_port=api_port,
        )
        self._advertiser = SsdpAdvertiser(self._uuid, server_ip, http_port)
        self._unsub = None

    async def start(self) -> None:
        # Register HTTP routes on the aiohttp app
        register_descriptor_routes(self._app, self._uuid, self._friendly_name, self._ip, self._http_port)
        register_cd_routes(self._app, self._cd_handler)
        register_cm_routes(self._app)
        register_audio_routes(self._app, self._cd_handler._tracks)

        # Start SSDP advertisement
        await self._advertiser.start()

        # Subscribe to library changes to update SystemUpdateID
        self._unsub = self._event_bus.on(
            EventType.LIBRARY_SCAN_COMPLETED,
            self._on_library_updated,
        )

        logger.info(
            "upnp_media_server_started",
            uuid=self._uuid,
            name=self._friendly_name,
            location=f"http://{self._ip}:{self._http_port}/upnp/device.xml",
        )

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()
        await self._advertiser.stop()
        logger.info("upnp_media_server_stopped")

    async def _on_library_updated(self, event) -> None:
        self._cd_handler.system_update_id += 1
        logger.debug("upnp_system_update_id_incremented", id=self._cd_handler.system_update_id)

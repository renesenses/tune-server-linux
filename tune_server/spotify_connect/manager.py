from __future__ import annotations

import socket
from pathlib import Path
from typing import Optional

import structlog

from tune_server.config import settings
from tune_server.event_bus import EventBus
from tune_server.spotify_connect.daemon import LibrespotDaemon
from tune_server.spotify_connect.relay import SpotifyConnectRelay
from tune_server.utils.network import get_local_ip

logger = structlog.get_logger()

DEFAULT_RELAY_PORT = 8082


def _default_device_name() -> str:
    host = socket.gethostname().split(".")[0]
    return f"Tune ({host})"


class SpotifyConnectManager:
    """Lifecycle of the Spotify Connect receiver: 1 device <-> 1 zone.

    Composition:
        - LibrespotDaemon: librespot subprocess in zeroconf mode
        - SpotifyConnectRelay: HTTP server that serves the daemon's PCM as WAV

    The zone integration (telling a target zone to play the relay URL) is
    exposed via `stream_url`; clients/server can route it through the existing
    play-by-URL infrastructure.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._daemon: LibrespotDaemon | None = None
        self._relay: SpotifyConnectRelay | None = None
        self._zone_id: int | None = None
        self._device_name: str = _default_device_name()

    @property
    def is_enabled(self) -> bool:
        return self._daemon is not None and self._daemon.is_running

    @property
    def stream_url(self) -> str | None:
        if not self._relay:
            return None
        return self._relay.url_for(get_local_ip())

    @property
    def status(self) -> dict:
        return {
            "enabled": self.is_enabled,
            "device_name": self._device_name,
            "zone_id": self._zone_id,
            "binary_available": self._binary_available(),
            "stream_url": self.stream_url,
        }

    def _binary_available(self) -> bool:
        from shutil import which
        path = settings.spotify_connect_binary or "librespot"
        return which(path) is not None or Path(path).exists()

    async def enable(self, zone_id: int, device_name: Optional[str] = None) -> None:
        if self.is_enabled:
            await self.disable()
        self._zone_id = zone_id
        if device_name:
            self._device_name = device_name
        binary = settings.spotify_connect_binary or "librespot"
        self._daemon = LibrespotDaemon(
            device_name=self._device_name,
            binary_path=binary,
            bitrate=settings.spotify_connect_bitrate,
            on_event=self._handle_event,
        )
        await self._daemon.start()
        self._relay = SpotifyConnectRelay(self._daemon, port=DEFAULT_RELAY_PORT)
        await self._relay.start()
        logger.info(
            "spotify_connect_enabled",
            zone_id=zone_id,
            name=self._device_name,
            stream_url=self.stream_url,
        )

    async def disable(self) -> None:
        if self._relay:
            await self._relay.stop()
            self._relay = None
        if self._daemon:
            await self._daemon.stop()
            self._daemon = None
        self._zone_id = None
        logger.info("spotify_connect_disabled")

    async def _handle_event(self, event: str, track_id: str | None, raw: str) -> None:
        logger.info("spotify_connect_event", event=event, track_id=track_id)

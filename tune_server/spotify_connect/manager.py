from __future__ import annotations

import socket
from pathlib import Path
from typing import Optional

import structlog

from tune_server.config import settings
from tune_server.event_bus import EventBus
from tune_server.spotify_connect.daemon import LibrespotDaemon

logger = structlog.get_logger()


def _default_device_name() -> str:
    host = socket.gethostname().split(".")[0]
    return f"Tune ({host})"


class SpotifyConnectManager:
    """Lifecycle of the Spotify Connect receiver: 1 device <-> 1 zone."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._daemon: LibrespotDaemon | None = None
        self._zone_id: int | None = None
        self._device_name: str = _default_device_name()

    @property
    def is_enabled(self) -> bool:
        return self._daemon is not None and self._daemon.is_running

    @property
    def status(self) -> dict:
        return {
            "enabled": self.is_enabled,
            "device_name": self._device_name,
            "zone_id": self._zone_id,
            "binary_available": self._binary_available(),
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
        logger.info("spotify_connect_enabled", zone_id=zone_id, name=self._device_name)

    async def disable(self) -> None:
        if self._daemon:
            await self._daemon.stop()
            self._daemon = None
        self._zone_id = None
        logger.info("spotify_connect_disabled")

    async def _handle_event(self, event: str, track_id: str | None, raw: str) -> None:
        # TODO v0.7.21+: route PCM chunks from daemon.read_pcm_chunk() into
        # the target zone's audio pipeline. Requires a new "live PCM source"
        # input on Player. For now, log only.
        logger.info("spotify_connect_event", event=event, track_id=track_id)

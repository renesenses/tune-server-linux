from __future__ import annotations

import socket
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import structlog

from tune_server.config import settings
from tune_server.event_bus import EventBus
from tune_server.models import AudioFormat, Source, Track
from tune_server.spotify_connect.daemon import LibrespotDaemon
from tune_server.spotify_connect.relay import SpotifyConnectRelay
from tune_server.utils.network import get_local_ip

if TYPE_CHECKING:
    from tune_server.zones.manager import ZoneManager

logger = structlog.get_logger()

DEFAULT_RELAY_PORT = 8082

# librespot event names that should trigger or stop zone playback.
_PLAY_EVENTS = {"playing", "started", "changed", "track_changed", "session_connected"}
_STOP_EVENTS = {"stopped", "session_disconnected", "end_of_track"}
_PAUSE_EVENTS = {"paused"}


def _default_device_name() -> str:
    host = socket.gethostname().split(".")[0]
    return f"Tune ({host})"


def _synthetic_track(stream_url: str) -> Track:
    """A placeholder Track that wraps the relay URL so existing player code
    can route it like any HTTP-streamed source."""
    return Track(
        title="Spotify Connect",
        artist_name="Spotify",
        album_title="Live",
        duration_ms=0,
        file_path=stream_url,
        format=AudioFormat.WAV,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        source=Source.SPOTIFY,
    )


class SpotifyConnectManager:
    """Lifecycle of the Spotify Connect receiver: 1 device <-> 1 zone.

    Composition:
        - LibrespotDaemon: librespot subprocess in zeroconf mode
        - SpotifyConnectRelay: HTTP server that serves the daemon's PCM as WAV

    When a librespot "playing" event fires, we route the relay URL into the
    target zone via the existing player (synthetic Track, file_path=URL).
    """

    def __init__(self, event_bus: EventBus, zone_manager: Optional["ZoneManager"] = None) -> None:
        self._event_bus = event_bus
        self._zone_manager = zone_manager
        self._daemon: LibrespotDaemon | None = None
        self._relay: SpotifyConnectRelay | None = None
        self._zone_id: int | None = None
        self._device_name: str = _default_device_name()
        self._zone_playing: bool = False  # latched: True after first play_url to avoid re-triggering

    def set_zone_manager(self, zone_manager: "ZoneManager") -> None:
        self._zone_manager = zone_manager

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
            "active": self._zone_playing,
        }

    def _binary_available(self) -> bool:
        from shutil import which
        path = settings.spotify_connect_binary or "librespot"
        return which(path) is not None or Path(path).exists()

    async def enable(self, zone_id: int, device_name: Optional[str] = None) -> None:
        if self.is_enabled:
            await self.disable()
        self._zone_id = zone_id
        self._zone_playing = False
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
        if self._zone_playing and self._zone_id is not None:
            await self._stop_zone()
        if self._relay:
            await self._relay.stop()
            self._relay = None
        if self._daemon:
            await self._daemon.stop()
            self._daemon = None
        self._zone_id = None
        self._zone_playing = False
        logger.info("spotify_connect_disabled")

    async def _handle_event(self, event: str, track_id: str | None, raw: str) -> None:
        evt = (event or "").lower()
        logger.info("spotify_connect_event", spotify_event=evt, track_id=track_id)
        if evt in _PLAY_EVENTS and not self._zone_playing:
            await self._start_zone()
        elif evt in _STOP_EVENTS:
            await self._stop_zone()
        elif evt in _PAUSE_EVENTS:
            await self._pause_zone()

    async def _start_zone(self) -> None:
        if self._zone_id is None or self._zone_manager is None or not self.stream_url:
            return
        zone = self._zone_manager.get_zone(self._zone_id)
        if zone is None:
            logger.warning("spotify_connect_zone_missing", zone_id=self._zone_id)
            return
        try:
            await zone.player.play(tracks=[_synthetic_track(self.stream_url)])
            self._zone_playing = True
            logger.info("spotify_connect_zone_playing", zone_id=self._zone_id, url=self.stream_url)
        except Exception as exc:
            logger.error("spotify_connect_zone_play_failed", zone_id=self._zone_id, error=str(exc))

    async def _pause_zone(self) -> None:
        if self._zone_id is None or self._zone_manager is None:
            return
        zone = self._zone_manager.get_zone(self._zone_id)
        if zone is None:
            return
        try:
            await zone.player.pause()
        except Exception as exc:
            logger.warning("spotify_connect_zone_pause_failed", error=str(exc))

    async def _stop_zone(self) -> None:
        if self._zone_id is None or self._zone_manager is None:
            return
        zone = self._zone_manager.get_zone(self._zone_id)
        if zone is None:
            return
        try:
            await zone.player.stop()
        except Exception as exc:
            logger.warning("spotify_connect_zone_stop_failed", error=str(exc))
        finally:
            self._zone_playing = False

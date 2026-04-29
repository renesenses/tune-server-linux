"""Sonos output target.

A Tune zone of OutputType.SONOS targets a single Sonos speaker (UID)
or, for a multi-room Sonos zone, a whole native group. Native Sonos
grouping is sample-accurate, so per-zone "follower" alignment is the
SonosManager's job — this OutputTarget just hands the speaker (or
group coordinator) the stream URL produced by Tune's HTTP streamer.

The HTTP streamer URL is provided to `start()` via stream_info; we
use it as the SetAVTransportURI payload via `SonosManager.play_uri`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import structlog

from tune_server.audio.formats import SONOS_CAPABILITIES, AudioCapabilities
from tune_server.models import AudioStreamInfo, Track
from tune_server.outputs.base import OutputTarget

if TYPE_CHECKING:
    from tune_server.zones.sonos_manager import SonosManager

logger = structlog.get_logger()


class SonosOutput(OutputTarget):
    """Single-speaker Sonos output. For multi-speaker Sonos zones the
    SonosManager wires the native group; this target just controls the
    coordinator (or solo speaker) and pushes URLs to it."""

    def __init__(
        self,
        manager: "SonosManager",
        speaker_uid: str,
    ) -> None:
        self._manager = manager
        self._speaker_uid = speaker_uid
        self._current_uri: Optional[str] = None

    @property
    def name(self) -> str:
        sp = self._manager.get(self._speaker_uid)
        if sp is None:
            return f"Sonos: {self._speaker_uid}"
        return f"Sonos: {getattr(sp, 'player_name', self._speaker_uid)}"

    @property
    def capabilities(self) -> AudioCapabilities:
        return SONOS_CAPABILITIES

    @property
    def is_available(self) -> bool:
        return self._manager.get(self._speaker_uid) is not None

    @property
    def is_direct_url(self) -> bool:
        # Sonos pulls audio from a URL we hand it — Tune's HTTP streamer
        # serves the bytes. No local PCM pipeline needed.
        return True

    def supports_direct_url(self, track: Track) -> bool:
        return True

    async def start(
        self, stream_info: AudioStreamInfo, track: Optional[Track] = None,
    ) -> None:
        url = getattr(stream_info, "url", None) or getattr(stream_info, "stream_url", None)
        if not url:
            logger.warning("sonos_start_no_url", uid=self._speaker_uid)
            return
        self._current_uri = url
        await self._manager.play_uri(self._speaker_uid, url)

    async def write(self, data: bytes) -> None:
        # Direct-URL output — Tune's pipeline never feeds bytes here.
        return None

    async def flush(self) -> None:
        return None

    async def pause(self) -> None:
        sp = self._manager.get(self._speaker_uid)
        if sp is None:
            return
        try:
            import asyncio
            await asyncio.to_thread(sp.pause)
        except Exception:
            pass

    async def resume(self) -> None:
        sp = self._manager.get(self._speaker_uid)
        if sp is None:
            return
        try:
            import asyncio
            await asyncio.to_thread(sp.play)
        except Exception:
            pass

    async def stop(self) -> None:
        await self._manager.stop_playback(self._speaker_uid)
        self._current_uri = None

    async def set_volume(self, volume: float) -> None:
        # Tune passes 0.0-1.0; Sonos is 0-100.
        await self._manager.set_volume(
            self._speaker_uid, int(volume * 100),
        )

    async def close(self) -> None:
        await self.stop()

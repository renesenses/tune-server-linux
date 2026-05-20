from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from tune_server.config import settings

if TYPE_CHECKING:
    from tune_server.playback.player import Player

logger = structlog.get_logger()


class CrossfadeHandler:
    """Manages crossfade logic for DLNA and local output.

    Owns the crossfade-related state that was previously scattered across
    Player.__init__.  The handler receives a back-reference to the Player
    so it can read ``_output``, ``_volume``, ``position_ms``, etc.
    """

    def __init__(self, player: Player) -> None:
        self._player = player
        # Settings
        self.enabled: bool = settings.crossfade_enabled
        self.duration: float = settings.crossfade_duration
        # Runtime state
        self._task: asyncio.Task | None = None
        self._original_volume: float | None = None  # to restore after fade

    # -- public properties used by external routes --------------------------

    @property
    def original_volume(self) -> float | None:
        return self._original_volume

    @original_volume.setter
    def original_volume(self, value: float | None) -> None:
        self._original_volume = value

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    # -- extracted methods --------------------------------------------------

    async def check(self) -> None:
        """Check if we should start crossfading to the next track.

        For local output: applies FFmpeg fade-out filter.
        For DLNA output: uses volume ramp (fade-out current, queue next,
        fade-in when next track starts).
        """
        p = self._player
        if p._audiophile_mode:
            return
        if not self.enabled or self.duration <= 0:
            return
        track = p.current_track
        if not track or not track.duration_ms:
            return

        from tune_server.models import Source
        if track.source == Source.RADIO:
            return

        remaining_ms = track.duration_ms - p.position_ms
        threshold_ms = int(self.duration * 1000)

        if remaining_ms <= threshold_ms and remaining_ms > threshold_ms - 500:
            if p._is_dlna_output():
                await self.start_dlna()
            elif p._pipeline:
                fade_filter = f"afade=t=out:st=0:d={self.duration}"
                p._channel_filter = fade_filter

    async def start_dlna(self) -> None:
        """Initiate DLNA crossfade: queue next track + volume fade-out.

        The approach:
        1. Ensure next track is queued via SetNextAVTransportURI
        2. Fade volume down over crossfade_duration seconds
        3. When _advance_track detects the next track started,
           finish_dlna fades volume back up
        """
        if self._task and not self._task.done():
            return  # already fading

        output = self._player._output
        if not output:
            return

        current_vol = await output.get_volume() if hasattr(output, 'get_volume') else None
        if current_vol is None:
            current_vol = self._player._volume

        self._original_volume = current_vol

        if not self._player._renderer_has_next:
            await self._player._preload_next()

        self._task = asyncio.create_task(
            self._dlna_fade_out(output, current_vol)
        )

    async def _dlna_fade_out(self, output, from_vol: float) -> None:
        """Fade DLNA volume down to 0 over crossfade_duration."""
        try:
            ok = await output.fade_volume(
                from_vol=from_vol,
                to_vol=0.0,
                duration=self.duration,
            )
            if not ok:
                logger.info(
                    "dlna_crossfade_skipped",
                    zone_id=self._player._zone_id,
                    reason="volume_control_not_supported",
                )
                self._original_volume = None
        except asyncio.CancelledError:
            logger.debug("dlna_fade_out_cancelled", zone_id=self._player._zone_id)
        except Exception:
            logger.exception("dlna_fade_out_error", zone_id=self._player._zone_id)
            self._original_volume = None

    async def finish_dlna(self) -> None:
        """Fade DLNA volume back up after track transition.

        Called from _advance_track when the next track starts playing
        and a crossfade was in progress.
        """
        if self._original_volume is None:
            return

        output = self._player._output
        if not output or not hasattr(output, 'fade_volume'):
            self._original_volume = None
            return

        target_vol = self._original_volume
        self._original_volume = None

        try:
            await output.fade_volume(
                from_vol=0.0,
                to_vol=target_vol,
                duration=self.duration,
            )
            logger.info(
                "dlna_crossfade_complete",
                zone_id=self._player._zone_id,
                restored_volume=round(target_vol, 2),
            )
        except Exception:
            logger.warning("dlna_fade_in_error", zone_id=self._player._zone_id)
            try:
                await output.set_volume(target_vol)
            except Exception:
                pass

    async def cancel(self) -> None:
        """Cancel any in-progress crossfade and restore volume."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._original_volume is not None and self._player._output:
            try:
                await self._player._output.set_volume(self._original_volume)
            except Exception:
                pass
            self._original_volume = None

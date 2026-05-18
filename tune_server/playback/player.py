from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from enum import StrEnum
from typing import Any, Callable, Coroutine, Optional

import structlog

from tune_server.config import settings
from tune_server.audio.formats import AudioCapabilities, LOCAL_CAPABILITIES
from pathlib import Path

from tune_server.audio.pipeline import AudioPipeline, create_pipeline
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import AudioFormat, AudioStreamInfo, PlaybackState, SignalPath, SignalPathStep, Source, Track
from tune_server.outputs.base import OutputTarget
from tune_server.playback.gapless import GaplessHandler
from tune_server.playback.queue import PlayQueue

StreamUrlResolver = Callable[[Track], Coroutine[Any, Any, Optional[str]]]
QueuePersistCallback = Callable[[list[Track], int], Coroutine[Any, Any, None]]


def cover_url_for_client(cover_path: str | None) -> str | None:
    """Transform cover_path to a URL the web client can load.

    - Local filesystem paths → ``/api/v1/library/artwork/<filename>``
    - HTTP(S) URLs (streaming CDN) → proxied through the artwork proxy
      so the client never hits an expired CDN token directly.
    """
    if not cover_path:
        return None
    if cover_path.startswith("http"):
        # Route through the artwork proxy which caches locally and
        # shields the client from expired Qobuz/Tidal/Deezer CDN tokens.
        from urllib.parse import quote
        return f"/api/v1/library/artwork/proxy?url={quote(cover_path, safe='')}"
    return f"/api/v1/library/artwork/{cover_path.split('/')[-1]}"


class PlayerHookEvent(StrEnum):
    """Lifecycle events plugins can hook into via Player.add_hook()."""
    BEFORE_TRACK = "before_track"   # fired with (zone_id, track) just before audio output starts
    AFTER_TRACK = "after_track"     # fired with (zone_id, track) when a track ends naturally
    PLAY = "play"                   # fired with (zone_id,) on resume / start
    PAUSE = "pause"                 # fired with (zone_id,) on pause
    STOP = "stop"                   # fired with (zone_id,) on stop


logger = structlog.get_logger()


class Player:
    """State machine for audio playback: stopped → playing → paused."""

    def __init__(self, zone_id: int, event_bus: EventBus) -> None:
        self._zone_id = zone_id
        self._event_bus = event_bus
        self._queue = PlayQueue()
        self._state = PlaybackState.STOPPED
        self._output: OutputTarget | None = None
        self._pipeline: AudioPipeline | None = None
        self._playback_task: asyncio.Task | None = None
        self._position_ms: int = 0
        self._position_start_time: float = 0
        self._volume: float = 0.5
        self._stream_url_resolver: StreamUrlResolver | None = None
        self._gapless: GaplessHandler | None = None
        self._queue_persist_cb: QueuePersistCallback | None = None
        self._volume_change_cb: Callable | None = None
        self._icy_poller_task: asyncio.Task | None = None
        self._radio_poller = None
        self._skip_in_progress = False
        self._lock = asyncio.Lock()
        self._signal_path: "SignalPath | None" = None
        self._renderer_has_next = False
        self._channel_filter: str | None = None
        # Crossfade
        self._crossfade_enabled = settings.crossfade_enabled
        self._crossfade_duration = settings.crossfade_duration
        self._crossfade_task: asyncio.Task | None = None
        self._crossfade_original_volume: float | None = None  # to restore after fade
        # Volume normalization
        self._normalization_enabled = False
        self._normalization_target = -14.0
        # Parametric equalizer
        self._eq_enabled: bool = False
        self._eq_bands: list[dict] = []  # each: {"freq": 60, "gain": 0, "q": 1.0}
        # Audiophile mode — disables all DSP processing for purest signal path
        self._audiophile_mode: bool = False
        # Quality preference per zone (used by streaming connectors)
        self._quality_preference: str = "max"
        # Plugin hooks: each PlayerHookEvent maps to a list of callables
        # invoked in registration order. Sync OR async, exceptions are
        # caught per-hook so a faulty plugin can't break playback.
        self._hooks: dict[PlayerHookEvent, list[Callable]] = defaultdict(list)

    @property
    def state(self) -> PlaybackState:
        return self._state

    @property
    def queue(self) -> PlayQueue:
        return self._queue

    @property
    def current_track(self) -> Optional[Track]:
        return self._queue.current

    @property
    def position_ms(self) -> int:
        pos = self._position_ms
        if self._state == PlaybackState.PLAYING:
            elapsed = (time.monotonic() - self._position_start_time) * 1000
            pos = int(pos + elapsed)
        track = self._queue.current
        if track and track.duration_ms and pos > track.duration_ms:
            return track.duration_ms
        return pos

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def signal_path(self) -> SignalPath | None:
        return self._signal_path

    @property
    def audiophile_mode(self) -> bool:
        return self._audiophile_mode

    @property
    def quality_preference(self) -> str:
        return self._quality_preference

    def set_quality_preference(self, quality: str) -> None:
        if quality not in ("max", "hires", "cd", "low"):
            raise ValueError(f"Invalid quality: {quality}")
        self._quality_preference = quality
        logger.info("quality_preference_changed", zone_id=self._zone_id, quality=quality)

    async def set_audiophile_mode(self, enabled: bool) -> None:
        """Enable or disable audiophile mode.

        When enabling: disables EQ, crossfade, normalization for a pure
        signal path. Volume is NOT changed — forcing 100% is dangerous
        on high-power DACs/amplifiers.
        """
        self._audiophile_mode = enabled
        if enabled:
            self._eq_enabled = False
            self._crossfade_enabled = False
            self._normalization_enabled = False
            logger.info("audiophile_mode_enabled", zone_id=self._zone_id)
        else:
            logger.info("audiophile_mode_disabled", zone_id=self._zone_id)

    def set_output(self, output: OutputTarget) -> None:
        self._output = output
        self._gapless = GaplessHandler(output.capabilities)

    def set_stream_url_resolver(self, resolver: StreamUrlResolver) -> None:
        self._stream_url_resolver = resolver

    def set_queue_persist_callback(self, cb: QueuePersistCallback) -> None:
        self._queue_persist_cb = cb

    def set_volume_change_callback(self, cb: Callable) -> None:
        self._volume_change_cb = cb

    def set_channel_filter(self, channel_filter: str | None) -> None:
        self._channel_filter = channel_filter

    def set_equalizer(self, enabled: bool, bands: list[dict]) -> None:
        """Set parametric EQ bands. Each band: {"freq": Hz, "gain": dB, "q": float}.

        Settings persist across tracks -- the EQ filter is applied at each
        _start_track() call via the pipeline's extra_filters parameter.
        """
        self._eq_enabled = enabled
        self._eq_bands = bands

    def get_equalizer(self) -> dict:
        """Return current EQ state."""
        return {"enabled": self._eq_enabled, "bands": self._eq_bands}

    def _build_eq_filter(self) -> str | None:
        """Build an FFmpeg -af equalizer filter chain from the current EQ bands.

        Only bands with non-zero gain are included. Returns None if EQ is
        disabled, all gains are zero, or audiophile mode is active.
        """
        if self._audiophile_mode:
            return None
        if not self._eq_enabled or not self._eq_bands:
            return None
        parts = []
        for band in self._eq_bands:
            gain = band.get("gain", 0)
            if gain == 0:
                continue
            freq = band.get("freq", 1000)
            q = band.get("q", 1.0)
            parts.append(f"equalizer=f={freq}:t=q:w={q}:g={gain}")
        return ",".join(parts) if parts else None

    def add_hook(self, event: PlayerHookEvent, fn: Callable) -> None:
        """Register a hook callable for a player lifecycle event.

        Plugins use this to react to playback transitions without subclassing
        Player. The callable receives positional args specific to the event
        (see PlayerHookEvent docstrings). Both sync and async callables are
        supported. Exceptions raised by hooks are logged and swallowed —
        they never break playback.
        """
        self._hooks[event].append(fn)

    def set_recording_hook(self, fn: Callable) -> None:
        """Compatibility shim — superseded by ``add_hook(BEFORE_TRACK, fn)``.

        Older callers (pre plugin-system) wired a single recording callback
        through this method. We keep it for one release so existing code
        keeps working; new code should use ``add_hook`` directly. The shim
        appends to the same hook list so the ``BEFORE_TRACK`` dispatch
        invokes ``fn`` exactly as before.
        """
        self._hooks[PlayerHookEvent.BEFORE_TRACK].append(fn)

    async def _fire_hook(self, event: PlayerHookEvent, *args, **kwargs) -> None:
        """Invoke all registered hooks for an event. Errors per-hook are isolated."""
        for fn in self._hooks.get(event, []):
            try:
                if asyncio.iscoroutinefunction(fn):
                    await fn(*args, **kwargs)
                else:
                    fn(*args, **kwargs)
            except Exception:
                logger.exception(
                    "player_hook_error",
                    hook_event=event.value,
                    fn=getattr(fn, "__qualname__", repr(fn)),
                )

    async def _cache_queue_covers(self, tracks: list[Track]) -> None:
        """Pre-cache HTTP cover URLs for queued tracks in background.

        Once all covers are cached locally the track objects are mutated
        in-place so subsequent queue reads return local paths instead of
        expiring CDN URLs.  A QUEUE_CHANGED event is emitted at the end
        so the web client refreshes cover art.
        """
        any_cached = False
        try:
            from tune_server.library.artwork import cache_cover_url
            for track in tracks:
                if track.cover_path and track.cover_path.startswith("http"):
                    cached = await asyncio.to_thread(cache_cover_url, track.cover_path)
                    if cached:
                        track.cover_path = cached
                        any_cached = True
        except Exception:
            logger.debug("cache_queue_covers_error", zone_id=self._zone_id)
        # Notify clients so they pick up the freshly cached local paths
        if any_cached:
            await self._event_bus.emit(Event(
                type=EventType.PLAYBACK_QUEUE_CHANGED,
                data={"zone_id": self._zone_id},
                source="cover_cache",
            ))

    def _is_dlna_output(self) -> bool:
        """Check if current output is a DLNA renderer."""
        if not self._output:
            return False
        return self._output.__class__.__name__ == "DlnaOutput"

    async def _check_crossfade(self):
        """Check if we should start crossfading to the next track.

        For local output: applies FFmpeg fade-out filter.
        For DLNA output: uses volume ramp (fade-out current, queue next,
        fade-in when next track starts).
        """
        if self._audiophile_mode:
            return
        if not self._crossfade_enabled or self._crossfade_duration <= 0:
            return
        track = self.current_track
        if not track or not track.duration_ms:
            return

        # Don't crossfade radio
        if track.source == Source.RADIO:
            return

        remaining_ms = track.duration_ms - self.position_ms
        threshold_ms = int(self._crossfade_duration * 1000)

        if remaining_ms <= threshold_ms and remaining_ms > threshold_ms - 500:
            if self._is_dlna_output():
                await self._start_dlna_crossfade()
            elif self._pipeline:
                # Local output: apply FFmpeg fade-out filter
                fade_filter = f"afade=t=out:st=0:d={self._crossfade_duration}"
                self._channel_filter = fade_filter

    async def _start_dlna_crossfade(self) -> None:
        """Initiate DLNA crossfade: queue next track + volume fade-out.

        The approach:
        1. Ensure next track is queued via SetNextAVTransportURI
        2. Fade volume down over crossfade_duration seconds
        3. When _advance_track detects the next track started,
           _finish_dlna_crossfade fades volume back up
        """
        if self._crossfade_task and not self._crossfade_task.done():
            return  # already fading

        if not self._output:
            return

        output = self._output
        # Read current volume from renderer (or use cached)
        current_vol = await output.get_volume() if hasattr(output, 'get_volume') else None
        if current_vol is None:
            current_vol = self._volume

        self._crossfade_original_volume = current_vol

        # Ensure next track is queued for gapless transition
        if not self._renderer_has_next:
            await self._preload_next()

        # Start async fade-out task
        self._crossfade_task = asyncio.create_task(
            self._dlna_fade_out(output, current_vol)
        )

    async def _dlna_fade_out(self, output, from_vol: float) -> None:
        """Fade DLNA volume down to 0 over crossfade_duration."""
        try:
            ok = await output.fade_volume(
                from_vol=from_vol,
                to_vol=0.0,
                duration=self._crossfade_duration,
            )
            if not ok:
                logger.info(
                    "dlna_crossfade_skipped",
                    zone_id=self._zone_id,
                    reason="volume_control_not_supported",
                )
                self._crossfade_original_volume = None
        except asyncio.CancelledError:
            logger.debug("dlna_fade_out_cancelled", zone_id=self._zone_id)
        except Exception:
            logger.exception("dlna_fade_out_error", zone_id=self._zone_id)
            self._crossfade_original_volume = None

    async def _finish_dlna_crossfade(self) -> None:
        """Fade DLNA volume back up after track transition.

        Called from _advance_track when the next track starts playing
        and a crossfade was in progress.
        """
        if self._crossfade_original_volume is None:
            return

        if not self._output or not hasattr(self._output, 'fade_volume'):
            self._crossfade_original_volume = None
            return

        target_vol = self._crossfade_original_volume
        self._crossfade_original_volume = None

        try:
            await self._output.fade_volume(
                from_vol=0.0,
                to_vol=target_vol,
                duration=self._crossfade_duration,
            )
            logger.info(
                "dlna_crossfade_complete",
                zone_id=self._zone_id,
                restored_volume=round(target_vol, 2),
            )
        except Exception:
            # Best-effort: if fade-in fails, force-set original volume
            logger.warning("dlna_fade_in_error", zone_id=self._zone_id)
            try:
                await self._output.set_volume(target_vol)
            except Exception:
                pass

    async def _cancel_crossfade(self) -> None:
        """Cancel any in-progress crossfade and restore volume."""
        if self._crossfade_task and not self._crossfade_task.done():
            self._crossfade_task.cancel()
            try:
                await self._crossfade_task
            except asyncio.CancelledError:
                pass
            self._crossfade_task = None

        # Restore volume if it was modified by crossfade
        if self._crossfade_original_volume is not None and self._output:
            try:
                await self._output.set_volume(self._crossfade_original_volume)
            except Exception:
                pass
            self._crossfade_original_volume = None

    async def _persist_queue(self) -> None:
        """Persist current queue state if callback is set."""
        if not self._queue_persist_cb:
            return
        await self._queue_persist_cb(self._queue.tracks, self._queue.position)

    async def _emit_playback_error(self, error_code: str, message: str, track: Track | None = None) -> None:
        data = {"zone_id": self._zone_id, "error": error_code, "message": message}
        if track:
            data["track_title"] = track.title
            data["source"] = track.source.value if track.source else None
            data["source_id"] = track.source_id
        await self._event_bus.emit(Event(
            type=EventType.PLAYBACK_ERROR, data=data, source="player",
        ))

    async def play(
        self,
        tracks: Optional[list[Track]] = None,
        start_position: int = 0,
    ) -> None:
        async with self._lock:
            # Stop any current playback BEFORE changing the queue to avoid race conditions
            # where the old _direct_url_monitor or _playback_loop advances into the new queue
            await self._stop_pipeline()

            if tracks:
                self._queue.set_tracks(tracks, start_position)
                await self._persist_queue()
                # Pre-cache HTTP cover URLs in background (Qobuz/Tidal CDN expiry)
                asyncio.create_task(self._cache_queue_covers(tracks))

            track = self._queue.current
            if not track:
                logger.warning("play_no_track", zone_id=self._zone_id)
                await self._emit_playback_error(
                    "no_track",
                    "Nothing to play — queue is empty",
                )
                return

            await self._start_track(track)

    async def _start_track(self, track: Track, seek_ms: int = 0) -> None:
        # Stop any current playback
        await self._stop_pipeline()

        # Cache HTTP cover URLs locally (Qobuz/Tidal CDN URLs expire)
        if track.cover_path and track.cover_path.startswith("http"):
            try:
                from tune_server.library.artwork import cache_cover_url
                cached = await asyncio.to_thread(cache_cover_url, track.cover_path)
                if cached:
                    track.cover_path = cached
            except Exception:
                logger.debug("cover_cache_failed", track=track.title)

        # Fallback: if track still has no cover, try extracting from the
        # audio file (local tracks) — this covers cases where the album
        # in the DB has no cover_path yet.
        if not track.cover_path and track.file_path and not track.file_path.startswith("http"):
            try:
                from tune_server.library.artwork import get_album_artwork
                cover = await asyncio.to_thread(get_album_artwork, track.file_path)
                if cover:
                    track.cover_path = cover
            except Exception:
                logger.debug("cover_extract_fallback_failed", track=track.title)

        if not self._output:
            logger.error("play_no_output", zone_id=self._zone_id)
            await self._emit_playback_error(
                "no_output",
                "No audio output configured for this zone",
                track,
            )
            return

        if hasattr(self._output, '_available') and not self._output._available:
            logger.warning("output_unavailable", zone_id=self._zone_id)
            await self._emit_playback_error("output_unavailable", "Output device is not available")
            return

        # Resolve stream URL for non-local tracks
        if not track.file_path and track.source_id and self._stream_url_resolver:
            try:
                url = await asyncio.wait_for(
                    self._stream_url_resolver(track),
                    timeout=settings.stream_url_resolve_timeout,
                )
            except asyncio.TimeoutError:
                logger.error("stream_url_timeout", track=track.title, source=track.source)
                await self._emit_playback_error("stream_url_timeout", f"Timed out resolving URL for '{track.title}'", track)
                await self._advance_track()
                return
            except Exception as e:
                logger.debug("stream_url_resolve_error", track=track.title, error=str(e))
                url = None
            if url:
                track.file_path = url
            else:
                logger.error("stream_url_resolve_failed", track=track.title, source=track.source)
                await self._emit_playback_error("stream_url_failed", f"Failed to resolve URL for '{track.title}'", track)
                await self._advance_track()
                return

        # Check if output can handle URL directly (e.g., DLNA renderer fetching from CDN)
        # or if output handles native DSD passthrough (renderer pulls DSF via HTTP)
        source_format = AudioFormat(track.format) if track.format else AudioFormat.FLAC
        _is_dst = track.file_path and track.file_path.lower().endswith(".dst")
        _native_dsd = (
            source_format == AudioFormat.DSD
            and getattr(self._output, "supports_native_dsd", False)
            and track.file_path
            and not track.file_path.startswith("http")
            and not _is_dst  # DST must be decompressed, can't passthrough
        )
        if (self._output.supports_direct_url(track) or _native_dsd) and seek_ms == 0:
            try:
                file_size = None
                if _native_dsd and track.file_path:
                    p = Path(track.file_path)
                    file_size = p.stat().st_size if p.exists() else None
                stream_info = AudioStreamInfo(
                    format=source_format,
                    sample_rate=track.sample_rate or 44100,
                    bit_depth=track.bit_depth or 16,
                    channels=track.channels or 2,
                    file_size=file_size,
                )
                await self._output.start(stream_info, track)
            except Exception:
                logger.exception("output_start_error", zone_id=self._zone_id)
                await self._emit_playback_error("output_error", f"Failed to start output for '{track.title}'", track)
                self._state = PlaybackState.STOPPED
                return

            # No pipeline needed — renderer fetches directly
            self._signal_path = self._build_signal_path(track, stream_info, passthrough_type="direct_url" if not _native_dsd else "native_dsd")
            self._state = PlaybackState.PLAYING
            self._position_ms = 0
            self._position_start_time = time.monotonic()

            await self._event_bus.emit(Event(
                type=EventType.PLAYBACK_STARTED,
                data={
                    "zone_id": self._zone_id,
                    "track_id": track.id,
                    "track_title": track.title,
                    "artist_name": track.artist_name,
                    "album_title": track.album_title,
                    "cover_path": cover_url_for_client(track.cover_path),
                    "duration_ms": track.duration_ms,
                    "position_ms": 0,
                },
                source="player",
            ))

            # Monitor track end for auto-advance
            self._playback_task = asyncio.create_task(self._direct_url_monitor(track))

            # For radio on DLNA: start metadata poller (RadioFrance API or ICY)
            # (the pipeline is bypassed so FFmpeg ICY parsing doesn't run)
            if track.source == Source.RADIO and track.file_path:
                icy_cb = self._make_icy_callback(track)
                from tune_server.streaming.radio_metadata import RadioMetadataPoller
                self._radio_poller = RadioMetadataPoller(
                    self._event_bus, self._zone_id, track_callback=icy_cb,
                )
                self._radio_poller.start(track.file_path)

            # Preload next track for gapless (SetNextAVTransportURI)
            await self._preload_next()
            return

        self._state = PlaybackState.BUFFERING

        capabilities = self._output.capabilities

        # Build audio pipeline with ICY callback for radio streams
        icy_cb = self._make_icy_callback(track) if track.source == Source.RADIO else None
        source_format = AudioFormat(track.format) if track.format else AudioFormat.FLAC

        # For local files on DLNA, the file will be served directly —
        # skip seek in pipeline to preserve passthrough (renderer handles seek via Range)
        pipeline_seek_ms = seek_ms
        if (seek_ms > 0
            and track.file_path
            and not track.file_path.startswith("http")
            and hasattr(self._output, 'is_direct_url')):
            pipeline_seek_ms = 0

        eq_filter = self._build_eq_filter()
        self._pipeline = create_pipeline(capabilities, icy_callback=icy_cb, channel_filter=self._channel_filter)
        try:
            stream_info = await asyncio.wait_for(
                self._pipeline.start(
                    file_path=track.file_path,
                    source_format=source_format,
                    sample_rate=track.sample_rate or 44100,
                    bit_depth=track.bit_depth or 16,
                    channels=track.channels or 2,
                    seek_ms=pipeline_seek_ms,
                    extra_filters=eq_filter,
                ),
                timeout=settings.pipeline_start_timeout,
            )
        except (asyncio.TimeoutError, Exception):
            logger.exception("pipeline_start_error", zone_id=self._zone_id, track=track.title)
            await self._emit_playback_error("pipeline_error", f"Failed to start pipeline for '{track.title}'", track)
            await self._stop_pipeline()
            self._state = PlaybackState.STOPPED
            return

        # Start output
        try:
            await self._output.start(stream_info, track)
        except Exception:
            logger.exception("output_start_error", zone_id=self._zone_id)
            await self._emit_playback_error("output_error", f"Failed to start output for '{track.title}'", track)
            await self._stop_pipeline()
            self._state = PlaybackState.STOPPED
            return

        # Determine passthrough type before potentially stopping pipeline
        passthrough_type = "file_passthrough" if self._pipeline and self._pipeline.is_passthrough else None

        # Start feeding output — skip pipeline loop if output serves files directly
        if self._output.is_direct_url:
            # File is served directly by HTTP streamer — pipeline is not needed.
            # Stop the pipeline and monitor the renderer instead.
            passthrough_type = "file_passthrough"
            await self._stop_pipeline()
            self._playback_task = asyncio.create_task(self._direct_url_monitor(track))
        else:
            self._playback_task = asyncio.create_task(self._playback_loop())
        self._signal_path = self._build_signal_path(track, stream_info, passthrough_type=passthrough_type)
        self._state = PlaybackState.PLAYING
        self._position_ms = seek_ms
        self._position_start_time = time.monotonic()

        await self._event_bus.emit(Event(
            type=EventType.PLAYBACK_STARTED,
            data={
                "zone_id": self._zone_id,
                "track_id": track.id,
                "track_title": track.title,
                "artist_name": track.artist_name,
                "album_title": track.album_title,
                "cover_path": cover_url_for_client(track.cover_path),
                "duration_ms": track.duration_ms,
                "position_ms": seek_ms,
            },
            source="player",
        ))

        # Radio metadata fallback: if ICY metadata hasn't arrived after 5s,
        # start the RadioFrance API poller as a backup source.
        if track.source == Source.RADIO and track.file_path and not self._radio_poller and icy_cb:
            _fallback_cb = icy_cb
            async def _radio_metadata_fallback():
                await asyncio.sleep(5)
                if self._state != PlaybackState.PLAYING:
                    return
                if not getattr(_fallback_cb, '_received', False):
                    from tune_server.streaming.radio_metadata import RadioMetadataPoller
                    self._radio_poller = RadioMetadataPoller(
                        self._event_bus, self._zone_id, track_callback=_fallback_cb,
                    )
                    self._radio_poller.start(track.file_path)
                    logger.info("radio_metadata_fallback_started", zone_id=self._zone_id)
            asyncio.create_task(_radio_metadata_fallback())

        # Preload next track for gapless transition
        await self._preload_next()

    async def _preload_next(self) -> None:
        """Preload the next track in queue for gapless playback."""
        if not self._output.capabilities.supports_gapless:
            return
        next_track = self._queue.peek_next()
        if not next_track:
            return
        # Resolve stream URL if needed
        if not next_track.file_path and next_track.source_id and self._stream_url_resolver:
            try:
                url = await asyncio.wait_for(self._stream_url_resolver(next_track), timeout=10)
            except (asyncio.TimeoutError, Exception):
                logger.warning("preload_url_resolve_failed", track=next_track.title)
                return
            if url:
                next_track.file_path = url
        if not next_track.file_path:
            return

        source_format = AudioFormat(next_track.format) if next_track.format else AudioFormat.FLAC
        _is_dst = next_track.file_path and next_track.file_path.lower().endswith(".dst")
        _native_dsd = (
            source_format == AudioFormat.DSD
            and getattr(self._output, "supports_native_dsd", False)
            and next_track.file_path
            and not next_track.file_path.startswith("http")
            and not _is_dst
        )

        # DLNA renderers: use SetNextAVTransportURI for gapless.
        if hasattr(self._output, 'set_next_track'):
            try:
                file_size = None
                is_local = next_track.file_path and not next_track.file_path.startswith("http")
                if is_local:
                    p = Path(next_track.file_path)
                    file_size = p.stat().st_size if p.exists() else None

                # Check if transcoding is needed
                pipeline_format = self._pipeline.stream_info.format if self._pipeline and self._pipeline.stream_info else None
                needs_transcode = is_local and pipeline_format and source_format != pipeline_format

                if needs_transcode:
                    # Preload via gapless handler to get transcoded data
                    if self._gapless:
                        await self._gapless.preload(next_track)
                        if self._gapless.has_next and self._gapless.next_stream_info:
                            transcode_info = self._gapless.next_stream_info
                            ok = await self._output.set_next_track(
                                transcode_info, next_track, gapless_handler=self._gapless,
                            )
                            if ok:
                                self._renderer_has_next = True
                                current = self._queue.current
                                if current:
                                    current.gapless_next = True
                                logger.info("gapless_next_set_transcoded", track=next_track.title,
                                            native=source_format.value, output=transcode_info.format.value)
                                return
                else:
                    next_info = AudioStreamInfo(
                        format=source_format,
                        sample_rate=next_track.sample_rate or 44100,
                        bit_depth=next_track.bit_depth or 16,
                        channels=next_track.channels or 2,
                        file_size=file_size,
                    )
                    ok = await self._output.set_next_track(next_info, next_track)
                    if ok:
                        self._renderer_has_next = True
                        current = self._queue.current
                        if current:
                            current.gapless_next = True
                        logger.info("gapless_next_set", track=next_track.title)
                        return
            except Exception:
                logger.exception("gapless_next_error", track=next_track.title)

        # For pipeline-based playback (local output): pre-decode the next track
        if self._gapless:
            if not self._gapless.has_next:
                await self._gapless.preload(next_track)
            current = self._queue.current
            if current and self._gapless.has_next:
                current.gapless_next = True

    async def _pre_buffer_next(self) -> None:
        """Pre-decode the start of the next track for seamless transitions.

        Called when the current track is within the pre-buffer threshold
        (10s before end). If gapless handler already has this track queued,
        this is a no-op.
        """
        if not self._gapless or not self._output:
            return
        next_track = self._queue.peek_next()
        if not next_track:
            return
        # Resolve stream URL if needed
        if not next_track.file_path and next_track.source_id and self._stream_url_resolver:
            try:
                url = await self._stream_url_resolver(next_track)
                if url:
                    next_track.file_path = url
            except Exception:
                return
        if next_track.file_path:
            self._gapless.pre_buffer_track(next_track)

    async def _playback_loop(self) -> None:
        try:
            while self._state in (PlaybackState.PLAYING, PlaybackState.BUFFERING, PlaybackState.PAUSED):
                # Wait while paused
                if self._state == PlaybackState.PAUSED:
                    await asyncio.sleep(0.1)
                    continue
                if not self._pipeline or not self._pipeline.output_buffer:
                    break  # Pipeline destroyed (seek/skip in progress)
                chunk = await self._pipeline.output_buffer.get()
                if chunk is None:
                    # Track finished — try gapless transition
                    if await self._try_gapless_transition():
                        continue  # Seamlessly continue the loop with new pipeline
                    break  # No gapless; fall through to _advance_track
                if self._output:
                    try:
                        await asyncio.wait_for(self._output.write(chunk), timeout=10)
                    except (asyncio.TimeoutError, IOError, ConnectionError, OSError):
                        logger.warning("output_write_failed", zone_id=self._zone_id)
                        # Track underrun for adaptive DLNA buffer sizing
                        if self._is_dlna_output() and hasattr(self._output, '_track_event'):
                            from tune_server.outputs.dlna_buffer_stats import EventKind
                            self._output._track_event(EventKind.UNDERRUN)
                        # Check if renderer is still playing (e.g. DLNA buffered data)
                        renderer_pos = await self._output.get_position_ms() if self._output else -1
                        if renderer_pos > 0:
                            # Renderer still playing — switch to direct_url monitor mode
                            logger.info("output_write_failed_but_renderer_playing",
                                        zone_id=self._zone_id, renderer_pos=renderer_pos)
                            break  # Exit pipeline loop, fall through to monitor below
                        # Retry once
                        await asyncio.sleep(1)
                        try:
                            await asyncio.wait_for(self._output.write(chunk), timeout=10)
                            logger.info("output_write_recovered", zone_id=self._zone_id)
                        except Exception:
                            logger.error("output_write_failed_final", zone_id=self._zone_id)
                            # Track disconnection for adaptive DLNA buffer sizing
                            if self._is_dlna_output() and hasattr(self._output, '_track_event'):
                                from tune_server.outputs.dlna_buffer_stats import EventKind
                                self._output._track_event(EventKind.DISCONNECTION)
                            await self._emit_playback_error("output_disconnected", "Output device disconnected")
                            self._state = PlaybackState.STOPPED
                            break

            if self._output:
                await self._output.flush()

            # Auto-advance to next track (or monitor renderer if it's still playing)
            if self._state == PlaybackState.PLAYING:
                # Check if renderer is still playing (pipeline broke but renderer buffered)
                # -2 means renderer stopped, don't switch to monitor
                renderer_pos = await self._output.get_position_ms() if self._output else -1
                if renderer_pos > 0 and renderer_pos != -2 and self._queue.current:
                    logger.info("switching_to_renderer_monitor", zone_id=self._zone_id)
                    await self._direct_url_monitor(self._queue.current)
                else:
                    await self._advance_track()

        except asyncio.CancelledError:
            logger.debug("playback_loop_cancelled", zone_id=self._zone_id)
        except Exception:
            logger.exception("playback_loop_error", zone_id=self._zone_id)
            await self._emit_playback_error("playback_loop_error", "Unexpected error during playback", self._queue.current)

    async def _direct_url_monitor(self, track: Track) -> None:
        """Monitor direct URL playback and auto-advance when track finishes."""
        try:
            duration_ms = track.duration_ms or 0
            is_radio = track.source == Source.RADIO if hasattr(track, 'source') else False

            # Radio streams play indefinitely — no position polling needed.
            # Polling via UPnP GetPositionInfo can disrupt some renderers.
            if is_radio:
                while self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED, PlaybackState.BUFFERING):
                    await asyncio.sleep(5)
                return

            # Debounce: require consecutive STOPPED polls before advancing.
            # Some renderers (e.g. DMP-A8) briefly report STOPPED during
            # initial buffering, causing premature track skip.
            stopped_count = 0
            stopped_threshold = 2  # need 2 consecutive STOPPED
            min_play_ms = 5000     # ignore STOPPED before 5s of playback
            cumulative_pos_ms = 0
            prev_pos_ms = 0        # previous poll position for gapless detection

            while self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED, PlaybackState.BUFFERING):
                # Poll faster (2s) when gapless is armed and nearing end,
                # normal rate (5s) otherwise to reduce UPnP traffic.
                if (self._renderer_has_next and duration_ms
                        and cumulative_pos_ms > duration_ms * 0.8):
                    await asyncio.sleep(2)
                else:
                    await asyncio.sleep(5)
                if self._state == PlaybackState.PAUSED:
                    continue

                # Prefer output-reported position (some DLNA renderers)
                output_pos = await self._output.get_position_ms() if self._output else -1

                # Update duration from output if available (BluOS reports totlen)
                if self._output and hasattr(self._output, 'reported_duration_ms'):
                    rd = self._output.reported_duration_ms
                    if rd > 0 and rd != duration_ms:
                        duration_ms = rd
                        if track.duration_ms != rd:
                            track.duration_ms = rd

                # Track cumulative position for minimum play duration check
                if output_pos >= 0:
                    cumulative_pos_ms = output_pos
                else:
                    cumulative_pos_ms += 1000  # estimate if no position

                # URI-based gapless detection: if the renderer's current
                # TrackURI differs from what we set, it auto-advanced to the
                # next track via SetNextAVTransportURI.  This is the most
                # reliable detection — works regardless of position heuristics.
                if (self._renderer_has_next
                        and self._output
                        and hasattr(self._output, 'has_uri_changed')
                        and self._output.has_uri_changed()):
                    logger.info(
                        "dlna_gapless_uri_transition_detected",
                        zone_id=self._zone_id,
                        track=track.title,
                    )
                    # Update the stored URI so subsequent checks work
                    if hasattr(self._output, 'sync_last_uri'):
                        self._output.sync_last_uri()
                    break

                # Position-based gapless detection (fallback): if the
                # renderer's position dropped from near-end to near-start,
                # the renderer has seamlessly transitioned to the next track.
                if (self._renderer_has_next
                        and output_pos >= 0
                        and duration_ms
                        and prev_pos_ms > duration_ms * 0.7
                        and output_pos < prev_pos_ms * 0.5):
                    logger.info(
                        "dlna_gapless_transition_detected",
                        zone_id=self._zone_id,
                        prev_pos=prev_pos_ms,
                        new_pos=output_pos,
                        duration=duration_ms,
                        track=track.title,
                    )
                    if self._output and hasattr(self._output, 'sync_last_uri'):
                        self._output.sync_last_uri()
                    break

                if output_pos >= 0:
                    prev_pos_ms = output_pos

                # -2 = renderer reports STOPPED
                if output_pos == -2 and not is_radio:
                    # Ignore early STOPPED (renderer still buffering)
                    if cumulative_pos_ms < min_play_ms:
                        logger.debug("dlna_stopped_ignored_early",
                                     zone_id=self._zone_id,
                                     pos_ms=cumulative_pos_ms,
                                     track=track.title)
                        stopped_count = 0
                        continue

                    # Instant transition if track played past 90% of duration
                    if duration_ms and cumulative_pos_ms >= duration_ms * 0.9:
                        logger.info("dlna_stopped_track_finished",
                                    zone_id=self._zone_id,
                                    pos_ms=cumulative_pos_ms,
                                    duration_ms=duration_ms,
                                    track=track.title)
                        break

                    # Otherwise debounce (mid-track transient STOPPED)
                    stopped_count += 1
                    if stopped_count >= stopped_threshold:
                        logger.info("dlna_stopped_confirmed",
                                    zone_id=self._zone_id,
                                    count=stopped_count,
                                    track=track.title)
                        break
                    else:
                        logger.debug("dlna_stopped_debounce",
                                     zone_id=self._zone_id,
                                     count=stopped_count,
                                     threshold=stopped_threshold,
                                     track=track.title)
                        continue
                else:
                    stopped_count = 0  # reset on any non-STOPPED poll

                pos = output_pos if output_pos >= 0 else self.position_ms

                # Trigger early pre-buffering when approaching track end
                if (duration_ms and self._gapless
                        and (duration_ms - pos) <= self._gapless.PRE_BUFFER_THRESHOLD_MS
                        and not self._gapless.has_next):
                    await self._pre_buffer_next()

                # DLNA crossfade: check if we're within crossfade threshold
                if (duration_ms and self._crossfade_enabled
                        and self._crossfade_duration > 0
                        and self._is_dlna_output()
                        and self._crossfade_original_volume is None
                        and (self._crossfade_task is None or self._crossfade_task.done())):
                    remaining = duration_ms - pos
                    threshold = int(self._crossfade_duration * 1000)
                    if 0 < remaining <= threshold and not is_radio:
                        await self._start_dlna_crossfade()

                if duration_ms and pos >= duration_ms:
                    break
                # No duration but output signals completion (pos >= 1)
                # Skip for radio (no duration, runs indefinitely)
                if not duration_ms and output_pos >= 1 and not is_radio:
                    break

            if self._state == PlaybackState.PLAYING:
                await self._advance_track()
        except asyncio.CancelledError:
            logger.debug("direct_url_monitor_cancelled", zone_id=self._zone_id)
        except Exception:
            logger.exception("direct_url_monitor_error", zone_id=self._zone_id)

    async def _try_gapless_transition(self) -> bool:
        """Attempt gapless transition to preloaded next track. Returns True if successful."""
        if not self._gapless or not self._gapless.has_next:
            return False

        # Check format compatibility — gapless only works if output format matches
        next_info = self._gapless.next_stream_info
        current_info = getattr(self._pipeline, 'stream_info', None)
        if not next_info or not current_info:
            return False
        if (next_info.sample_rate != current_info.sample_rate or
                next_info.bit_depth != current_info.bit_depth or
                next_info.channels != current_info.channels):
            logger.info("gapless_format_mismatch", zone_id=self._zone_id)
            return False

        # Take the preloaded pipeline and pre-buffered chunks
        new_pipeline, new_stream_info, pre_buffered_chunks = self._gapless.take_pipeline()
        if not new_pipeline:
            return False

        # Stop old pipeline (but NOT the output — that's the gapless part)
        old_pipeline = self._pipeline
        self._pipeline = new_pipeline

        if old_pipeline:
            await old_pipeline.stop()

        # Advance queue
        next_track = self._queue.next()
        if not next_track:
            return False

        self._position_ms = 0
        self._position_start_time = time.monotonic()

        # Flush pre-buffered chunks to the output immediately for seamless transition
        if pre_buffered_chunks and self._output:
            for chunk in pre_buffered_chunks:
                try:
                    await asyncio.wait_for(self._output.write(chunk), timeout=5)
                except Exception:
                    logger.warning("gapless_prebuffer_write_failed", zone_id=self._zone_id)
                    break

        await self._persist_queue()

        await self._event_bus.emit(Event(
            type=EventType.PLAYBACK_TRACK_CHANGED,
            data={
                "zone_id": self._zone_id,
                "track_id": next_track.id,
                "track_title": next_track.title,
                "artist_name": next_track.artist_name,
                "album_title": next_track.album_title,
                "cover_path": cover_url_for_client(next_track.cover_path),
            },
            source="player",
        ))

        logger.info("gapless_transition", track=next_track.title,
                     pre_buffered_chunks=len(pre_buffered_chunks))

        # Preload the NEXT next track
        await self._preload_next()
        return True

    async def _advance_track(self) -> None:
        # Check if current track failed prematurely (stream URL expired)
        # Skip for outputs that handle URLs directly
        current = self._queue.current
        if (current and current.source_id and self._stream_url_resolver
                and current.duration_ms and self.position_ms < current.duration_ms * 0.9
                and self._output and not self._output.supports_direct_url(current)):
            try:
                new_url = await asyncio.wait_for(
                    self._stream_url_resolver(current), timeout=10
                )
                if new_url and new_url != current.file_path:
                    current.file_path = new_url
                    logger.info("stream_url_refreshed", track=current.title)
                    await self._start_track(current, seek_ms=self.position_ms)
                    return
            except Exception:
                logger.warning("stream_url_refresh_failed", track=current.title)

        next_track = self._queue.next()
        if next_track:
            await self._persist_queue()
            logger.info("advance_track_path",
                        zone_id=self._zone_id,
                        has_next=self._renderer_has_next,
                        state=self._state.value,
                        crossfade_active=self._crossfade_original_volume is not None,
                        track=next_track.title)
            await self._event_bus.emit(Event(
                type=EventType.PLAYBACK_TRACK_CHANGED,
                data={
                    "zone_id": self._zone_id,
                    "track_id": next_track.id,
                    "track_title": next_track.title,
                    "artist_name": next_track.artist_name,
                    "album_title": next_track.album_title,
                    "cover_path": cover_url_for_client(next_track.cover_path),
                },
                source="player",
            ))

            if self._renderer_has_next:
                self._renderer_has_next = False
                self._position_ms = 0
                self._position_start_time = time.monotonic()
                source_format = AudioFormat(next_track.format) if next_track.format else AudioFormat.FLAC
                stream_info = AudioStreamInfo(
                    format=source_format,
                    sample_rate=next_track.sample_rate or 44100,
                    bit_depth=next_track.bit_depth or 16,
                    channels=next_track.channels or 2,
                )
                self._signal_path = self._build_signal_path(next_track, stream_info, passthrough_type="direct_url")
                self._playback_task = asyncio.create_task(self._direct_url_monitor(next_track))
                await self._preload_next()
                # DLNA crossfade: fade volume back up after track transition
                if self._crossfade_original_volume is not None:
                    asyncio.create_task(self._finish_dlna_crossfade())
                logger.info("gapless_soft_advance", track=next_track.title)
                return

            # Non-gapless DLNA advance: also fade in if crossfade was active
            if self._crossfade_original_volume is not None:
                asyncio.create_task(self._finish_dlna_crossfade())

            await self._start_track(next_track)
        else:
            # Restore volume if crossfade was in progress (last track in queue)
            if self._crossfade_original_volume is not None and self._output:
                try:
                    await self._output.set_volume(self._crossfade_original_volume)
                except Exception:
                    pass
                self._crossfade_original_volume = None
            self._state = PlaybackState.STOPPED
            self._position_ms = 0
            await self._event_bus.emit(Event(
                type=EventType.PLAYBACK_STOPPED,
                data={"zone_id": self._zone_id},
                source="player",
            ))

    async def pause(self) -> None:
        async with self._lock:
            if self._state != PlaybackState.PLAYING:
                return

            self._position_ms = self.position_ms
            self._state = PlaybackState.PAUSED

            if self._output:
                await self._output.pause()

            await self._event_bus.emit(Event(
                type=EventType.PLAYBACK_PAUSED,
                data={"zone_id": self._zone_id, "position_ms": self._position_ms},
                source="player",
            ))

    async def resume(self) -> None:
        async with self._lock:
            if self._state != PlaybackState.PAUSED:
                return

            self._state = PlaybackState.PLAYING
            self._position_start_time = time.monotonic()

            if self._output:
                await self._output.resume()

            await self._event_bus.emit(Event(
                type=EventType.PLAYBACK_RESUMED,
                data={"zone_id": self._zone_id},
                source="player",
            ))

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_pipeline()
            self._state = PlaybackState.STOPPED
            self._position_ms = 0
            self._signal_path = None

            if self._output:
                await self._output.stop()

            await self._event_bus.emit(Event(
                type=EventType.PLAYBACK_STOPPED,
                data={"zone_id": self._zone_id},
                source="player",
            ))

    async def skip_next(self) -> None:
        if self._skip_in_progress:
            return
        self._skip_in_progress = True
        try:
            async with self._lock:
                next_track = self._queue.next()
                if next_track:
                    await self._persist_queue()
                    await self._stop_pipeline()
                    await self._event_bus.emit(Event(
                        type=EventType.PLAYBACK_TRACK_CHANGED,
                        data={
                            "zone_id": self._zone_id,
                            "track_id": next_track.id,
                            "track_title": next_track.title,
                            "artist_name": next_track.artist_name,
                            "album_title": next_track.album_title,
                            "cover_path": cover_url_for_client(next_track.cover_path),
                        },
                        source="player",
                    ))
                    await self._start_track(next_track)
                else:
                    await self._stop_pipeline()
                    self._state = PlaybackState.STOPPED
                    self._position_ms = 0
                    if self._output:
                        await self._output.stop()
                    await self._event_bus.emit(Event(
                        type=EventType.PLAYBACK_STOPPED,
                        data={"zone_id": self._zone_id},
                        source="player",
                    ))
        finally:
            self._skip_in_progress = False

    async def skip_previous(self) -> None:
        if self._skip_in_progress:
            return
        self._skip_in_progress = True
        try:
            # CD player behavior: if past 3 seconds, restart current track;
            # if in the first 3 seconds, go to previous track
            if self.position_ms > 3000 and self._queue.current:
                track = self._queue.current
                async with self._lock:
                    await self._stop_pipeline()
                    await self._start_track(track)
                return

            async with self._lock:
                prev_track = self._queue.previous()
                if prev_track:
                    await self._persist_queue()
                    await self._stop_pipeline()
                    await self._event_bus.emit(Event(
                        type=EventType.PLAYBACK_TRACK_CHANGED,
                        data={
                            "zone_id": self._zone_id,
                            "track_id": prev_track.id,
                            "track_title": prev_track.title,
                            "artist_name": prev_track.artist_name,
                            "album_title": prev_track.album_title,
                            "cover_path": cover_url_for_client(prev_track.cover_path),
                        },
                        source="player",
                    ))
                    await self._start_track(prev_track)
        finally:
            self._skip_in_progress = False

    async def seek(self, position_ms: int) -> None:
        if self._skip_in_progress:
            return
        track = self._queue.current
        if not track:
            return

        if position_ms < 0:
            position_ms = 0
        if track.duration_ms and position_ms > track.duration_ms:
            position_ms = track.duration_ms

        # Try native output seek first (DLNA Seek(REL_TIME) — no pipeline restart)
        output = self._output
        if output and self._state == PlaybackState.PLAYING:
            try:
                if await output.seek(position_ms):
                    self._position_ms = position_ms
                    self._position_start_time = time.monotonic()
                    logger.info("native_seek", zone_id=self._zone_id, position_ms=position_ms)
                    return
            except (AttributeError, RuntimeError):
                logger.debug("seek_output_unavailable", zone_id=self._zone_id)

        # Fallback: full pipeline restart
        self._skip_in_progress = True
        try:
            was_playing = self._state == PlaybackState.PLAYING
            self._state = PlaybackState.BUFFERING
            self._position_ms = position_ms
            self._position_start_time = time.monotonic()

            # Re-resolve stream URL for streaming tracks (CDN tokens expire)
            if (track.source and track.source != Source.LOCAL
                    and track.source != Source.RADIO
                    and track.source_id and self._stream_url_resolver):
                try:
                    url = await asyncio.wait_for(
                        self._stream_url_resolver(track),
                        timeout=settings.stream_url_resolve_timeout,
                    )
                    if url:
                        track.file_path = url
                except Exception:
                    logger.debug("seek_url_refresh_failed", track=track.title)

            async with self._lock:
                await self._stop_pipeline()
            if was_playing:
                await self._start_track(track, seek_ms=position_ms)
            else:
                self._state = PlaybackState.STOPPED
        finally:
            self._skip_in_progress = False

    async def set_volume(self, volume: float) -> None:
        async with self._lock:
            self._volume = max(0.0, min(1.0, volume))
            if self._output:
                await self._output.set_volume(self._volume)
            if self._volume_change_cb:
                await self._volume_change_cb(self._volume)

            await self._event_bus.emit(Event(
                type=EventType.ZONE_VOLUME_CHANGED,
                data={"zone_id": self._zone_id, "volume": self._volume},
                source="player",
            ))

    def _build_signal_path(self, track: Track, stream_info: AudioStreamInfo, passthrough_type: str | None = None) -> SignalPath:
        """Build a SignalPath describing the complete audio chain."""
        steps: list[SignalPathStep] = []
        src_fmt = track.format or "unknown"
        src_rate = track.sample_rate or 44100
        src_depth = track.bit_depth or 16
        src_ch = track.channels or 2
        out_type = self._output.__class__.__name__ if self._output else "unknown"
        output_name = getattr(self._output, "name", out_type)

        def fmt_name(f) -> str:
            """Get clean format name from AudioFormat enum or string."""
            return f.value.upper() if hasattr(f, 'value') else str(f).upper()

        # Step 1: Source
        source_label = track.source.value.capitalize() if track.source else "Local"
        if track.source == Source.RADIO:
            source_detail = "Live radio stream"
        elif track.source in (Source.TIDAL, Source.QOBUZ):
            source_detail = f"Streaming CDN ({track.source.value.capitalize()})"
        else:
            source_detail = "Local file" if track.file_path and not track.file_path.startswith("http") else "Network"

        steps.append(SignalPathStep(
            stage="source",
            description=f"{source_label}: {fmt_name(src_fmt)}",
            format=fmt_name(src_fmt),
            sample_rate=src_rate,
            bit_depth=src_depth,
            channels=src_ch,
            detail=source_detail,
        ))

        # Step 2: Transport / Processing
        bit_perfect = False
        if passthrough_type == "direct_url":
            steps.append(SignalPathStep(
                stage="transport",
                description="Direct URL Passthrough",
                detail="Renderer fetches audio directly from source — zero processing",
            ))
            bit_perfect = True
        elif passthrough_type == "native_dsd":
            steps.append(SignalPathStep(
                stage="transport",
                description="Native DSD Passthrough",
                detail="DSD stream served bit-perfect to DSD-capable renderer",
            ))
            bit_perfect = True
        elif passthrough_type == "file_passthrough":
            steps.append(SignalPathStep(
                stage="transport",
                description="File Passthrough",
                format=fmt_name(stream_info.format),
                sample_rate=stream_info.sample_rate,
                bit_depth=stream_info.bit_depth,
                channels=stream_info.channels,
                detail="Native format streamed without re-encoding",
            ))
            bit_perfect = True
        else:
            # Transcoded
            out_fmt = fmt_name(stream_info.format)
            resampled = stream_info.sample_rate != src_rate or stream_info.bit_depth != src_depth
            desc_parts = []
            if fmt_name(src_fmt) != out_fmt:
                desc_parts.append(f"{fmt_name(src_fmt)} → {out_fmt}")
            if resampled:
                desc_parts.append(f"{src_rate//1000}kHz/{src_depth}bit → {stream_info.sample_rate//1000}kHz/{stream_info.bit_depth}bit")
            description = "Transcode: " + ", ".join(desc_parts) if desc_parts else "Transcode"

            steps.append(SignalPathStep(
                stage="decode",
                description=description,
                format=out_fmt,
                sample_rate=stream_info.sample_rate,
                bit_depth=stream_info.bit_depth,
                channels=stream_info.channels,
                detail="FFmpeg decode + re-encode" + (" with resampling" if resampled else ""),
            ))

        # Step 3: Output
        steps.append(SignalPathStep(
            stage="output",
            description=f"{output_name}",
            format=fmt_name(stream_info.format),
            sample_rate=stream_info.sample_rate,
            bit_depth=stream_info.bit_depth,
            channels=stream_info.channels,
            detail=out_type.replace("Output", "").replace("output", ""),
        ))

        # Summary
        lossy_formats = {"mp3", "aac", "ogg", "opus", "wma"}
        is_lossy = (src_fmt or "").lower() in lossy_formats
        if bit_perfect:
            summary = f"Bit-Perfect — {fmt_name(src_fmt)} {src_rate//1000}kHz/{src_depth}bit"
        elif is_lossy:
            summary = f"Lossy — {fmt_name(src_fmt)} {src_rate//1000}kHz/{src_depth}bit"
        else:
            summary = f"Transcoded — {fmt_name(stream_info.format)} {stream_info.sample_rate//1000}kHz/{stream_info.bit_depth}bit"

        # Add pipeline decisions if available
        decisions: list[str] = []
        checksum: str | None = None
        checksum_verified: bool | None = None

        if self._pipeline:
            decisions = self._pipeline.decisions
            if self._pipeline.source_hash:
                checksum = self._pipeline.source_hash
                checksum_verified = self._pipeline.source_hash == self._pipeline.output_hash

        return SignalPath(
            bit_perfect=bit_perfect,
            steps=steps,
            summary=summary,
            decisions=decisions,
            checksum=checksum,
            checksum_verified=checksum_verified,
        )

    def _make_icy_callback(self, track: Track):
        """Create a callback that updates the current track with ICY metadata."""
        zone_id = self._zone_id
        event_bus = self._event_bus
        station_name = track.title  # original station name
        station_cover = track.cover_path  # original station logo (fallback)

        def on_icy_metadata(meta: dict[str, str]) -> None:
            on_icy_metadata._received = True
            current = self._queue.current
            if not current or current.source != Source.RADIO:
                return

            icy_title = meta.get("title", "")
            icy_artist = meta.get("artist", "")

            if not icy_title and not icy_artist:
                return

            # Update the track metadata in-place
            if icy_artist:
                current.artist_name = icy_artist
                current.title = icy_title or station_name
            else:
                # No separator found — put raw title in album_title
                current.title = icy_title
                current.artist_name = station_name

            current.album_title = station_name  # keep station name accessible

            # Update cover: use RadioFrance cover if available, else station logo
            cover_url = meta.get("cover_url")
            current.cover_path = cover_url or station_cover

            logger.info("icy_metadata_update", station=station_name, title=icy_title, artist=icy_artist)

            # Emit event so WebSocket clients can update
            event_bus.emit_nowait(Event(
                type=EventType.PLAYBACK_METADATA,
                data={
                    "zone_id": zone_id,
                    "title": current.title,
                    "artist_name": current.artist_name,
                    "album_title": current.album_title,
                    "cover_path": cover_url_for_client(current.cover_path),
                    "source": "radio",
                },
                source="player",
            ))

        return on_icy_metadata

    async def _poll_icy_metadata(self, stream_url: str, callback) -> None:
        """Poll ICY metadata from a radio stream URL independently of the audio pipeline."""
        import aiohttp
        try:
            headers = {"Icy-MetaData": "1", "User-Agent": "TuneServer/1.0"}
            timeout = aiohttp.ClientTimeout(total=0)  # no timeout for radio
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(stream_url, headers=headers) as resp:
                    metaint = int(resp.headers.get("icy-metaint", "0"))
                    if metaint == 0:
                        return  # no ICY support

                    audio_bytes = 0
                    async for chunk in resp.content.iter_any():
                        if self._state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                            break

                        # Skip audio data, just track byte count for metadata boundaries
                        audio_bytes += len(chunk)
                        while audio_bytes >= metaint:
                            # We've passed a metadata boundary — read next metadata block
                            audio_bytes -= metaint
                            # Read the metadata length byte
                            meta_len_data = await resp.content.readexactly(1)
                            meta_len = meta_len_data[0] * 16
                            if meta_len > 0:
                                meta_data = await resp.content.readexactly(meta_len)
                                meta_str = meta_data.decode("utf-8", errors="ignore").rstrip("\x00")
                                # Parse StreamTitle='Artist - Title';
                                for part in meta_str.split(";"):
                                    part = part.strip()
                                    if part.lower().startswith("streamtitle="):
                                        title = part.split("=", 1)[1].strip("'\"")
                                        if title:
                                            artist, track_title = "", title
                                            if " - " in title:
                                                artist, track_title = title.split(" - ", 1)
                                            callback({"title": track_title.strip(), "artist": artist.strip()})
        except asyncio.CancelledError:
            logger.debug("icy_poller_cancelled", zone_id=self._zone_id)
        except Exception:
            logger.debug("icy_poller_stopped", zone_id=self._zone_id)

    async def _stop_pipeline(self) -> None:
        self._renderer_has_next = False
        # Cancel any in-progress crossfade and restore volume
        await self._cancel_crossfade()
        # Stop ICY/radio metadata pollers
        if self._icy_poller_task:
            self._icy_poller_task.cancel()
            self._icy_poller_task = None
        if self._radio_poller:
            self._radio_poller.stop()
            self._radio_poller = None

        if self._gapless:
            await self._gapless.cancel()

        if self._playback_task:
            # Don't cancel ourselves when called from within the playback task
            # (e.g. _direct_url_monitor → _advance_track → _start_track → here)
            if self._playback_task is not asyncio.current_task():
                self._playback_task.cancel()
                try:
                    await self._playback_task
                except asyncio.CancelledError:
                    logger.debug("playback_task_cancelled", zone_id=self._zone_id)
            self._playback_task = None

        if self._pipeline:
            await self._pipeline.stop()
            self._pipeline = None

    async def cleanup(self) -> None:
        await self.stop()
        if self._output:
            await self._output.close()

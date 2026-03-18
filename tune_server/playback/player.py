from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Coroutine, Optional

import structlog

from tune_server.config import settings
from tune_server.audio.formats import AudioCapabilities, LOCAL_CAPABILITIES
from pathlib import Path

from tune_server.audio.pipeline import AudioPipeline
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import AudioFormat, AudioStreamInfo, PlaybackState, Source, Track
from tune_server.outputs.base import OutputTarget
from tune_server.playback.gapless import GaplessHandler
from tune_server.playback.queue import PlayQueue

StreamUrlResolver = Callable[[Track], Coroutine[Any, Any, Optional[str]]]
QueuePersistCallback = Callable[[list[Track], int], Coroutine[Any, Any, None]]

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
        if self._state == PlaybackState.PLAYING:
            elapsed = (time.monotonic() - self._position_start_time) * 1000
            return int(self._position_ms + elapsed)
        return self._position_ms

    @property
    def volume(self) -> float:
        return self._volume

    def set_output(self, output: OutputTarget) -> None:
        self._output = output
        self._gapless = GaplessHandler(output.capabilities)

    def set_stream_url_resolver(self, resolver: StreamUrlResolver) -> None:
        self._stream_url_resolver = resolver

    def set_queue_persist_callback(self, cb: QueuePersistCallback) -> None:
        self._queue_persist_cb = cb

    def set_volume_change_callback(self, cb: Callable) -> None:
        self._volume_change_cb = cb

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
        # Stop any current playback BEFORE changing the queue to avoid race conditions
        # where the old _direct_url_monitor or _playback_loop advances into the new queue
        await self._stop_pipeline()

        if tracks:
            self._queue.set_tracks(tracks, start_position)
            await self._persist_queue()

        track = self._queue.current
        if not track:
            logger.warning("play_no_track", zone_id=self._zone_id)
            return

        await self._start_track(track)

    async def _start_track(self, track: Track, seek_ms: int = 0) -> None:
        # Stop any current playback
        await self._stop_pipeline()

        if not self._output:
            logger.error("play_no_output", zone_id=self._zone_id)
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
            except Exception:
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
        _native_dsd = (
            source_format == AudioFormat.DSD
            and getattr(self._output, "supports_native_dsd", False)
            and track.file_path
            and not track.file_path.startswith("http")
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
            self._state = PlaybackState.PLAYING
            self._position_ms = 0
            self._position_start_time = time.monotonic()

            await self._event_bus.emit(Event(
                type=EventType.PLAYBACK_STARTED,
                data={
                    "zone_id": self._zone_id,
                    "track_id": track.id,
                    "track_title": track.title,
                },
                source="player",
            ))

            # Monitor track end for auto-advance
            self._playback_task = asyncio.create_task(self._direct_url_monitor(track))
            # Preload next track for gapless (SetNextAVTransportURI)
            await self._preload_next()
            return

        self._state = PlaybackState.BUFFERING

        capabilities = self._output.capabilities

        # Build audio pipeline
        source_format = AudioFormat(track.format) if track.format else AudioFormat.FLAC
        self._pipeline = AudioPipeline(capabilities)
        try:
            stream_info = await asyncio.wait_for(
                self._pipeline.start(
                    file_path=track.file_path,
                    source_format=source_format,
                    sample_rate=track.sample_rate or 44100,
                    bit_depth=track.bit_depth or 16,
                    channels=track.channels or 2,
                    seek_ms=seek_ms,
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

        # Start feeding output
        self._playback_task = asyncio.create_task(self._playback_loop())

        self._state = PlaybackState.PLAYING
        self._position_ms = seek_ms
        self._position_start_time = time.monotonic()

        await self._event_bus.emit(Event(
            type=EventType.PLAYBACK_STARTED,
            data={
                "zone_id": self._zone_id,
                "track_id": track.id,
                "track_title": track.title,
            },
            source="player",
        ))

        # Preload next track for gapless transition
        await self._preload_next()

    async def _preload_next(self) -> None:
        """Preload the next track in queue for gapless playback."""
        if not self._gapless:
            return
        next_track = self._queue.peek_next()
        if not next_track:
            return
        # Resolve stream URL if needed
        if not next_track.file_path and next_track.source_id and self._stream_url_resolver:
            url = await self._stream_url_resolver(next_track)
            if url:
                next_track.file_path = url
        if next_track.file_path:
            await self._gapless.preload(next_track)

    async def _playback_loop(self) -> None:
        try:
            while self._state in (PlaybackState.PLAYING, PlaybackState.BUFFERING):
                chunk = await self._pipeline.output_buffer.get()
                if chunk is None:
                    # Track finished — try gapless transition
                    if await self._try_gapless_transition():
                        continue  # Seamlessly continue the loop with new pipeline
                    break  # No gapless; fall through to _advance_track
                if self._output:
                    await self._output.write(chunk)

            if self._output:
                await self._output.flush()

            # Auto-advance to next track
            if self._state == PlaybackState.PLAYING:
                await self._advance_track()

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("playback_loop_error", zone_id=self._zone_id)
            await self._emit_playback_error("playback_loop_error", "Unexpected error during playback", self._queue.current)

    async def _direct_url_monitor(self, track: Track) -> None:
        """Monitor direct URL playback and auto-advance when track finishes."""
        try:
            duration_ms = track.duration_ms or 0
            if not duration_ms:
                # No duration info — cannot auto-advance, wait for user action or stop
                while self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                    await asyncio.sleep(2)
                return

            while self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED, PlaybackState.BUFFERING):
                await asyncio.sleep(1)
                if self._state == PlaybackState.PAUSED:
                    continue
                if self.position_ms >= duration_ms:
                    break

            if self._state == PlaybackState.PLAYING:
                await self._advance_track()
        except asyncio.CancelledError:
            pass
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

        # Take the preloaded pipeline
        new_pipeline, new_stream_info = self._gapless.take_pipeline()
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

        await self._persist_queue()

        await self._event_bus.emit(Event(
            type=EventType.PLAYBACK_TRACK_CHANGED,
            data={
                "zone_id": self._zone_id,
                "track_id": next_track.id,
                "track_title": next_track.title,
            },
            source="player",
        ))

        logger.info("gapless_transition", track=next_track.title)

        # Preload the NEXT next track
        await self._preload_next()
        return True

    async def _advance_track(self) -> None:
        next_track = self._queue.next()
        if next_track:
            await self._persist_queue()
            logger.info("advancing_track", title=next_track.title)
            await self._event_bus.emit(Event(
                type=EventType.PLAYBACK_TRACK_CHANGED,
                data={
                    "zone_id": self._zone_id,
                    "track_id": next_track.id,
                    "track_title": next_track.title,
                },
                source="player",
            ))
            await self._start_track(next_track)
        else:
            self._state = PlaybackState.STOPPED
            self._position_ms = 0
            await self._event_bus.emit(Event(
                type=EventType.PLAYBACK_STOPPED,
                data={"zone_id": self._zone_id},
                source="player",
            ))

    async def pause(self) -> None:
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

    async def skip_next(self) -> None:
        next_track = self._queue.next()
        if next_track:
            await self._persist_queue()
            await self._start_track(next_track)
        else:
            await self.stop()

    async def skip_previous(self) -> None:
        prev_track = self._queue.previous()
        if prev_track:
            await self._persist_queue()
            await self._start_track(prev_track)

    async def seek(self, position_ms: int) -> None:
        track = self._queue.current
        if not track:
            return

        if position_ms < 0:
            position_ms = 0
        if track.duration_ms and position_ms > track.duration_ms:
            position_ms = track.duration_ms

        was_playing = self._state == PlaybackState.PLAYING
        await self._stop_pipeline()

        if was_playing:
            await self._start_track(track, seek_ms=position_ms)

    async def set_volume(self, volume: float) -> None:
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

    async def _stop_pipeline(self) -> None:
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
                    pass
            self._playback_task = None

        if self._pipeline:
            await self._pipeline.stop()
            self._pipeline = None

    async def cleanup(self) -> None:
        await self.stop()
        if self._output:
            await self._output.close()

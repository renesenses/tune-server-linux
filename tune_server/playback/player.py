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
from tune_server.models import AudioFormat, AudioStreamInfo, PlaybackState, SignalPath, SignalPathStep, Source, Track
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
        self._icy_poller_task: asyncio.Task | None = None
        self._radio_poller = None
        self._skip_in_progress = False
        self._lock = asyncio.Lock()
        self._signal_path: "SignalPath | None" = None

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

    @property
    def signal_path(self) -> SignalPath | None:
        return self._signal_path

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
        async with self._lock:
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
        self._pipeline = AudioPipeline(capabilities, icy_callback=icy_cb)
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

        # Start feeding output — skip pipeline loop if output serves files directly
        if self._output.is_direct_url:
            # File is served directly by HTTP streamer — pipeline is not needed.
            # Stop the pipeline and monitor the renderer instead.
            await self._stop_pipeline()
            self._playback_task = asyncio.create_task(self._direct_url_monitor(track))
        else:
            self._playback_task = asyncio.create_task(self._playback_loop())

        passthrough_type = "file_passthrough" if self._pipeline and self._pipeline.is_passthrough else None
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
            },
            source="player",
        ))

        # Preload next track for gapless transition
        await self._preload_next()

    async def _preload_next(self) -> None:
        """Preload the next track in queue for gapless playback."""
        if not self._gapless or not self._output.capabilities.supports_gapless:
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
        if next_track.file_path:
            await self._gapless.preload(next_track)

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
            pass
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
            stopped_threshold = 3  # need 3 consecutive STOPPED (≈3s)
            min_play_ms = 5000     # ignore STOPPED before 5s of playback
            cumulative_pos_ms = 0

            while self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED, PlaybackState.BUFFERING):
                await asyncio.sleep(10)  # 10s — aggressive polling disrupts some renderers (DMP-A8)
                if self._state == PlaybackState.PAUSED:
                    continue

                # Prefer output-reported position (some DLNA renderers)
                output_pos = await self._output.get_position_ms() if self._output else -1

                # Track cumulative position for minimum play duration check
                if output_pos >= 0:
                    cumulative_pos_ms = output_pos
                else:
                    cumulative_pos_ms += 1000  # estimate if no position

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

                if duration_ms and pos >= duration_ms:
                    break
                # No duration but output signals completion (pos >= 1)
                # Skip for radio (no duration, runs indefinitely)
                if not duration_ms and output_pos >= 1 and not is_radio:
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
        # Check if current track failed prematurely (stream URL expired)
        # Skip for outputs that handle URLs directly
        current = self._queue.current
        if (current and current.source_id and self._stream_url_resolver
                and current.duration_ms and self.position_ms < current.duration_ms * 0.9
                and not self._output.supports_direct_url(current)):
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
            if next_track:
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
                async with self._lock:
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
            if prev_track:
                await self._event_bus.emit(Event(
                    type=EventType.PLAYBACK_TRACK_CHANGED,
                    data={
                        "zone_id": self._zone_id,
                        "track_id": prev_track.id,
                        "track_title": prev_track.title,
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
        if self._output and self._state == PlaybackState.PLAYING:
            if await self._output.seek(position_ms):
                self._position_ms = position_ms
                self._position_start_time = time.monotonic()
                logger.info("native_seek", zone_id=self._zone_id, position_ms=position_ms)
                return

        # Fallback: full pipeline restart
        self._skip_in_progress = True
        try:
            async with self._lock:
                was_playing = self._state == PlaybackState.PLAYING
                await self._stop_pipeline()
            if was_playing:
                await self._start_track(track, seek_ms=position_ms)
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
        if bit_perfect:
            summary = f"Bit-Perfect — {fmt_name(src_fmt)} {src_rate//1000}kHz/{src_depth}bit"
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
                    "cover_path": current.cover_path,
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
            pass
        except Exception:
            logger.debug("icy_poller_stopped", zone_id=self._zone_id)

    async def _stop_pipeline(self) -> None:
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
                    pass
            self._playback_task = None

        if self._pipeline:
            await self._pipeline.stop()
            self._pipeline = None

    async def cleanup(self) -> None:
        await self.stop()
        if self._output:
            await self._output.close()

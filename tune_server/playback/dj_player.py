"""DualDeckPlayer — two independent audio pipelines for DJ-style mixing."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Callable, Coroutine, Optional

import structlog

from tune_server.audio.formats import AudioCapabilities, LOCAL_CAPABILITIES
from tune_server.audio.mixer import AudioMixer
from tune_server.audio.pipeline import AudioPipeline
from tune_server.config import settings
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import AudioFormat, AudioStreamInfo, Track
from tune_server.outputs.base import OutputTarget

StreamUrlResolver = Callable[[Track], Coroutine[Any, Any, Optional[str]]]

logger = structlog.get_logger()

# Default PCM format for the mixing pipeline (always decode to PCM for mixing)
_MIX_SAMPLE_RATE = 44100
_MIX_BIT_DEPTH = 16
_MIX_CHANNELS = 2


class DeckState:
    """State for a single DJ deck."""

    def __init__(self) -> None:
        self.pipeline: AudioPipeline | None = None
        self.track: Track | None = None
        self.stream_info: AudioStreamInfo | None = None
        self.position_ms: int = 0
        self.position_start_time: float = 0.0
        self.playing: bool = False

    @property
    def current_position_ms(self) -> int:
        if self.playing and self.position_start_time > 0:
            elapsed = (time.monotonic() - self.position_start_time) * 1000
            return int(self.position_ms + elapsed)
        return self.position_ms

    def to_dict(self) -> dict | None:
        if not self.track:
            return None
        return {
            "title": self.track.title,
            "artist": self.track.artist_name,
            "album": self.track.album_title,
            "cover": self.track.cover_path,
            "duration_ms": self.track.duration_ms,
            "position_ms": self.current_position_ms,
            "playing": self.playing,
            "track_id": self.track.id,
        }


class DualDeckPlayer:
    """Manages two independent audio pipelines for DJ mixing.

    Each deck has its own AudioPipeline (FFmpeg decode). A central mixing
    loop reads PCM chunks from both pipelines, applies per-deck gain,
    sums them, and writes the result to a single OutputTarget.
    """

    def __init__(
        self,
        zone_id: int,
        output: OutputTarget,
        event_bus: EventBus,
        stream_url_resolver: StreamUrlResolver | None = None,
        capabilities: AudioCapabilities | None = None,
    ) -> None:
        self._zone_id = zone_id
        self._output = output
        self._event_bus = event_bus
        self._stream_url_resolver = stream_url_resolver
        self._capabilities = capabilities or LOCAL_CAPABILITIES

        self._deck_a = DeckState()
        self._deck_b = DeckState()

        # Gains (0.0 to 1.0)
        self._gain_a: float = 1.0
        self._gain_b: float = 0.0
        self._active_deck: str = "a"

        # Mixer (created once output format is known)
        self._mixer: AudioMixer | None = None

        # Mixing loop
        self._running = False
        self._playback_task: asyncio.Task | None = None

        # Crossfade
        self._crossfade_task: asyncio.Task | None = None
        self._crossfading = False

        # Output stream info (set when mixing starts)
        self._output_started = False
        self._mix_stream_info: AudioStreamInfo | None = None

    @property
    def active_deck(self) -> str:
        return self._active_deck

    @property
    def gain_a(self) -> float:
        return self._gain_a

    @property
    def gain_b(self) -> float:
        return self._gain_b

    @property
    def crossfading(self) -> bool:
        return self._crossfading

    @property
    def deck_a(self) -> DeckState:
        return self._deck_a

    @property
    def deck_b(self) -> DeckState:
        return self._deck_b

    def _get_deck(self, deck: str) -> DeckState:
        return self._deck_a if deck == "a" else self._deck_b

    async def load_deck(self, deck: str, track: Track) -> dict:
        """Load a track onto deck A or B. Resolves streaming URLs if needed.

        Returns deck info dict on success.
        """
        deck_state = self._get_deck(deck)

        # Stop any existing pipeline on this deck
        if deck_state.pipeline:
            await deck_state.pipeline.stop()
            deck_state.pipeline = None

        # Resolve stream URL for non-local tracks
        if not track.file_path and track.source_id and self._stream_url_resolver:
            try:
                url = await asyncio.wait_for(
                    self._stream_url_resolver(track),
                    timeout=settings.stream_url_resolve_timeout,
                )
            except asyncio.TimeoutError:
                logger.error("dj_stream_url_timeout", deck=deck, track=track.title)
                raise RuntimeError(f"Timed out resolving URL for '{track.title}'")
            except Exception as exc:
                logger.error("dj_stream_url_error", deck=deck, track=track.title, error=str(exc))
                raise RuntimeError(f"Failed to resolve URL for '{track.title}'")
            if url:
                track.file_path = url
            else:
                raise RuntimeError(f"No URL resolved for '{track.title}'")

        if not track.file_path:
            raise RuntimeError(f"Track '{track.title}' has no file path")

        # Determine output format for the mix pipeline.
        # We always decode to PCM WAV for mixing (LOCAL_CAPABILITIES forces WAV).
        mix_caps = AudioCapabilities(
            formats={AudioFormat.WAV},
            max_sample_rate=min(
                track.sample_rate or _MIX_SAMPLE_RATE,
                self._capabilities.max_sample_rate,
            ),
            max_bit_depth=min(
                track.bit_depth or _MIX_BIT_DEPTH,
                self._capabilities.max_bit_depth,
            ),
        )

        source_format = AudioFormat(track.format) if track.format else AudioFormat.FLAC

        pipeline = AudioPipeline(mix_caps)
        try:
            stream_info = await asyncio.wait_for(
                pipeline.start(
                    file_path=track.file_path,
                    source_format=source_format,
                    sample_rate=track.sample_rate or _MIX_SAMPLE_RATE,
                    bit_depth=track.bit_depth or _MIX_BIT_DEPTH,
                    channels=track.channels or _MIX_CHANNELS,
                ),
                timeout=settings.pipeline_start_timeout,
            )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.exception("dj_pipeline_start_error", deck=deck, track=track.title)
            raise RuntimeError(f"Failed to start pipeline for '{track.title}': {exc}")

        deck_state.pipeline = pipeline
        deck_state.track = track
        deck_state.stream_info = stream_info
        deck_state.position_ms = 0
        deck_state.position_start_time = 0.0
        deck_state.playing = False

        logger.info(
            "dj_deck_loaded",
            deck=deck,
            track=track.title,
            format=stream_info.format.value if stream_info.format else "unknown",
            sample_rate=stream_info.sample_rate,
            bit_depth=stream_info.bit_depth,
        )

        # Ensure mixer exists with correct format
        self._ensure_mixer(stream_info)

        # Start mixing loop if not already running
        if not self._running:
            await self._start_mixing()

        result = deck_state.to_dict()
        return result or {}

    def _ensure_mixer(self, stream_info: AudioStreamInfo) -> None:
        """Create or update the mixer to match the stream format."""
        if self._mixer and (
            self._mixer.sample_rate == stream_info.sample_rate
            and self._mixer.bit_depth == stream_info.bit_depth
            and self._mixer.channels == stream_info.channels
        ):
            return

        self._mixer = AudioMixer(
            sample_rate=stream_info.sample_rate,
            bit_depth=stream_info.bit_depth,
            channels=stream_info.channels,
        )
        self._mix_stream_info = stream_info
        logger.info(
            "dj_mixer_created",
            sample_rate=stream_info.sample_rate,
            bit_depth=stream_info.bit_depth,
            channels=stream_info.channels,
        )

    async def _start_mixing(self) -> None:
        """Start the output and the mixing loop."""
        if not self._mix_stream_info:
            logger.error("dj_start_mixing_no_stream_info")
            return

        if not self._output_started:
            try:
                await self._output.start(self._mix_stream_info)
                self._output_started = True
            except Exception:
                logger.exception("dj_output_start_error")
                return

        self._running = True
        self._playback_task = asyncio.create_task(self._mixing_loop())
        logger.info("dj_mixing_started", zone_id=self._zone_id)

    async def _try_get(self, pipeline: AudioPipeline | None) -> bytes | None:
        """Try to get a chunk from a pipeline buffer with a short timeout.

        Returns None if no pipeline, buffer is empty after timeout, or
        the pipeline has finished (buffer closed).
        """
        if not pipeline or not pipeline.output_buffer:
            return None

        try:
            chunk = await asyncio.wait_for(pipeline.output_buffer.get(), timeout=0.02)
            return chunk
        except asyncio.TimeoutError:
            return None

    async def _mixing_loop(self) -> None:
        """Main loop: read from both pipelines, mix, write to output."""
        try:
            while self._running:
                deck_a_playing = self._deck_a.playing and self._deck_a.pipeline is not None
                deck_b_playing = self._deck_b.playing and self._deck_b.pipeline is not None

                if not deck_a_playing and not deck_b_playing:
                    # Neither deck is playing, sleep briefly and check again
                    await asyncio.sleep(0.02)
                    continue

                chunk_a: bytes | None = None
                chunk_b: bytes | None = None

                if deck_a_playing:
                    chunk_a = await self._try_get(self._deck_a.pipeline)
                    # None from get() when buffer is closed means track ended
                    if chunk_a is None and self._deck_a.pipeline:
                        if self._deck_a.pipeline.output_buffer.is_empty and self._deck_a.pipeline.output_buffer._closed:
                            self._deck_a.playing = False
                            logger.info("dj_deck_a_finished")
                            await self._event_bus.emit(Event(
                                type=EventType.PLAYBACK_TRACK_CHANGED,
                                data={
                                    "zone_id": self._zone_id,
                                    "deck": "a",
                                    "event": "deck_finished",
                                },
                                source="dj_player",
                            ))

                if deck_b_playing:
                    chunk_b = await self._try_get(self._deck_b.pipeline)
                    if chunk_b is None and self._deck_b.pipeline:
                        if self._deck_b.pipeline.output_buffer.is_empty and self._deck_b.pipeline.output_buffer._closed:
                            self._deck_b.playing = False
                            logger.info("dj_deck_b_finished")
                            await self._event_bus.emit(Event(
                                type=EventType.PLAYBACK_TRACK_CHANGED,
                                data={
                                    "zone_id": self._zone_id,
                                    "deck": "b",
                                    "event": "deck_finished",
                                },
                                source="dj_player",
                            ))

                if chunk_a is None and chunk_b is None:
                    await asyncio.sleep(0.005)
                    continue

                if not self._mixer:
                    await asyncio.sleep(0.01)
                    continue

                mixed = self._mixer.mix(chunk_a, chunk_b, self._gain_a, self._gain_b)
                if mixed and self._output:
                    try:
                        await asyncio.wait_for(self._output.write(mixed), timeout=5)
                    except asyncio.TimeoutError:
                        logger.warning("dj_output_write_timeout", zone_id=self._zone_id)
                    except (IOError, ConnectionError, OSError):
                        logger.warning("dj_output_write_failed", zone_id=self._zone_id)
                        break

        except asyncio.CancelledError:
            logger.debug("dj_mixing_loop_cancelled", zone_id=self._zone_id)
        except Exception:
            logger.exception("dj_mixing_loop_error", zone_id=self._zone_id)
        finally:
            self._running = False

    async def play_deck(self, deck: str) -> None:
        """Start playback on a deck (after loading)."""
        deck_state = self._get_deck(deck)
        if not deck_state.pipeline or not deck_state.track:
            raise RuntimeError(f"Deck {deck} has no track loaded")

        deck_state.playing = True
        deck_state.position_start_time = time.monotonic()

        # If this is the active deck, ensure its gain is up
        if self._active_deck == deck:
            if deck == "a":
                self._gain_a = 1.0
            else:
                self._gain_b = 1.0

        # Start mixing loop if not already running
        if not self._running:
            await self._start_mixing()

        logger.info("dj_deck_play", deck=deck, track=deck_state.track.title)

    async def pause_deck(self, deck: str) -> None:
        """Pause playback on a deck."""
        deck_state = self._get_deck(deck)
        if deck_state.playing:
            deck_state.position_ms = deck_state.current_position_ms
            deck_state.playing = False
            deck_state.position_start_time = 0.0
            logger.info("dj_deck_paused", deck=deck)

    async def set_deck_gain(self, deck: str, gain: float) -> None:
        """Set the gain for a specific deck (0.0 to 1.0)."""
        gain = max(0.0, min(1.0, gain))
        if deck == "a":
            self._gain_a = gain
        else:
            self._gain_b = gain

    async def set_crossfader(self, position: float) -> None:
        """Set crossfader position (-1.0 = full A, 0.0 = center, 1.0 = full B)."""
        position = max(-1.0, min(1.0, position))
        # Map crossfader position to gains
        # -1.0: A=1.0, B=0.0 | 0.0: A=1.0, B=1.0 | 1.0: A=0.0, B=1.0
        if position <= 0:
            self._gain_a = 1.0
            self._gain_b = 1.0 + position  # -1→0, 0→1
        else:
            self._gain_a = 1.0 - position  # 0→1, 1→0
            self._gain_b = 1.0

    async def start_crossfade(
        self,
        duration_seconds: float = 5.0,
        curve: str = "linear",
    ) -> None:
        """Gradually transition from active deck to inactive deck.

        Args:
            duration_seconds: How long the crossfade takes.
            curve: 'linear' or 'equal_power' (cos/sin).
        """
        # Cancel any ongoing crossfade
        if self._crossfade_task and not self._crossfade_task.done():
            self._crossfade_task.cancel()
            try:
                await self._crossfade_task
            except asyncio.CancelledError:
                pass

        target_deck = "b" if self._active_deck == "a" else "a"
        target_state = self._get_deck(target_deck)

        if not target_state.pipeline or not target_state.track:
            raise RuntimeError(f"Target deck {target_deck} has no track loaded")

        # Ensure target deck is playing
        if not target_state.playing:
            target_state.playing = True
            target_state.position_start_time = time.monotonic()

        self._crossfading = True
        self._crossfade_task = asyncio.create_task(
            self._crossfade_worker(duration_seconds, curve, target_deck)
        )

        logger.info(
            "dj_crossfade_started",
            from_deck=self._active_deck,
            to_deck=target_deck,
            duration=duration_seconds,
            curve=curve,
        )

    async def _crossfade_worker(
        self,
        duration: float,
        curve: str,
        target_deck: str,
    ) -> None:
        """Background task that updates gains over time for crossfade."""
        try:
            steps = max(int(duration * 50), 10)  # ~50 updates per second
            interval = duration / steps

            from_deck = self._active_deck

            for i in range(steps + 1):
                t = i / steps  # 0.0 to 1.0

                if curve == "equal_power":
                    # Equal power crossfade: constant total power
                    gain_out = math.cos(t * math.pi / 2)
                    gain_in = math.sin(t * math.pi / 2)
                else:
                    # Linear crossfade
                    gain_out = 1.0 - t
                    gain_in = t

                if from_deck == "a":
                    self._gain_a = gain_out
                    self._gain_b = gain_in
                else:
                    self._gain_b = gain_out
                    self._gain_a = gain_in

                if i < steps:
                    await asyncio.sleep(interval)

            # Crossfade complete: swap active deck
            self._active_deck = target_deck
            self._crossfading = False

            # Set gains to final state
            if target_deck == "a":
                self._gain_a = 1.0
                self._gain_b = 0.0
            else:
                self._gain_a = 0.0
                self._gain_b = 1.0

            logger.info(
                "dj_crossfade_complete",
                active_deck=self._active_deck,
            )

            await self._event_bus.emit(Event(
                type=EventType.PLAYBACK_TRACK_CHANGED,
                data={
                    "zone_id": self._zone_id,
                    "event": "crossfade_complete",
                    "active_deck": self._active_deck,
                },
                source="dj_player",
            ))

        except asyncio.CancelledError:
            self._crossfading = False
            logger.debug("dj_crossfade_cancelled")
        except Exception:
            self._crossfading = False
            logger.exception("dj_crossfade_error")

    async def stop(self) -> None:
        """Stop both decks and the mixing loop."""
        self._running = False

        # Cancel crossfade
        if self._crossfade_task and not self._crossfade_task.done():
            self._crossfade_task.cancel()
            try:
                await self._crossfade_task
            except asyncio.CancelledError:
                pass
            self._crossfade_task = None

        # Cancel mixing loop
        if self._playback_task and not self._playback_task.done():
            self._playback_task.cancel()
            try:
                await self._playback_task
            except asyncio.CancelledError:
                pass
            self._playback_task = None

        # Stop pipelines
        if self._deck_a.pipeline:
            await self._deck_a.pipeline.stop()
            self._deck_a.pipeline = None
        if self._deck_b.pipeline:
            await self._deck_b.pipeline.stop()
            self._deck_b.pipeline = None

        self._deck_a.playing = False
        self._deck_b.playing = False

        # Stop output
        if self._output_started:
            try:
                await self._output.stop()
            except Exception:
                logger.debug("dj_output_stop_error")
            self._output_started = False

        self._crossfading = False
        self._mixer = None

        logger.info("dj_player_stopped", zone_id=self._zone_id)

    def status(self) -> dict:
        """Return the current DJ player status."""
        return {
            "active_deck": self._active_deck,
            "gain_a": round(self._gain_a, 3),
            "gain_b": round(self._gain_b, 3),
            "crossfading": self._crossfading,
            "mixing": self._running,
            "deck_a": self._deck_a.to_dict(),
            "deck_b": self._deck_b.to_dict(),
        }

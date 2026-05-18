from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import structlog

from tune_server.audio.buffer import AsyncRingBuffer
from tune_server.audio.decoder import FFmpegDecoder, IcyMetadataCallback
from tune_server.audio.formats import AudioCapabilities, can_passthrough
from tune_server.models import AudioFormat, AudioStreamInfo

logger = structlog.get_logger()

CHUNK_SIZE = 32768  # 32KB

# 44.1 kHz family rates, preferred for DSD conversion (avoids SRC artefacts)
_441_FAMILY = [352800, 176400, 88200, 44100]


def _best_dsd_rate(max_rate: int) -> int:
    """Pick the highest 44.1kHz-family rate that fits within max_rate."""
    for rate in _441_FAMILY:
        if rate <= max_rate:
            return rate
    return 44100


class AudioPipeline:
    """
    Audio pipeline: decode → [resample] → [encode] → output buffer.

    If the output can handle the source format natively, does bit-perfect passthrough.
    Otherwise, decodes to PCM and re-encodes to a compatible format.
    """

    def __init__(
        self,
        target_capabilities: AudioCapabilities,
        icy_callback: IcyMetadataCallback | None = None,
        channel_filter: str | None = None,
    ) -> None:
        self._capabilities = target_capabilities
        self._decisions: list[str] = []
        self._source_hash: str | None = None
        self._output_hash: str | None = None
        self._icy_callback = icy_callback
        self._channel_filter = channel_filter
        self._decoder: FFmpegDecoder | None = None
        self._output_buffer = AsyncRingBuffer(max_chunks=512)
        self._pipeline_task: asyncio.Task | None = None
        self._passthrough = False
        self._stream_info: AudioStreamInfo | None = None

    @property
    def output_buffer(self) -> AsyncRingBuffer:
        return self._output_buffer

    @property
    def stream_info(self) -> AudioStreamInfo | None:
        return self._stream_info

    @property
    def is_passthrough(self) -> bool:
        return self._passthrough

    @property
    def decisions(self) -> list[str]:
        return self._decisions

    @property
    def source_hash(self) -> str | None:
        return self._source_hash

    @property
    def output_hash(self) -> str | None:
        return self._output_hash

    async def start(
        self,
        file_path: str,
        source_format: AudioFormat,
        sample_rate: int,
        bit_depth: int,
        channels: int,
        seek_ms: int = 0,
        extra_filters: str | None = None,
    ) -> AudioStreamInfo:
        # Check if we can do passthrough
        self._passthrough = can_passthrough(
            source_format, sample_rate, bit_depth, self._capabilities
        )
        self._decisions.append(f"can_passthrough={self._passthrough} (format={source_format}, rate={sample_rate}, depth={bit_depth})")

        _is_url = file_path.startswith("http://") or file_path.startswith("https://")
        if _is_url:
            self._passthrough = False
            self._decisions.append("passthrough=False (HTTP/HTTPS stream requires decode)")

        if seek_ms > 0:
            self._passthrough = False
            self._decisions.append(f"passthrough=False (seeking to {seek_ms}ms)")

        if self._channel_filter:
            self._passthrough = False
            self._decisions.append(f"passthrough=False (channel_filter={self._channel_filter})")

        if extra_filters:
            self._passthrough = False
            self._decisions.append(f"passthrough=False (extra_filters={extra_filters})")

        # Resample policy check
        from tune_server.config import settings as _settings
        if not self._passthrough and _settings.resample_policy == "never":
            needs_resample = sample_rate > self._capabilities.max_sample_rate or bit_depth > self._capabilities.max_bit_depth
            if needs_resample and source_format in self._capabilities.formats:
                self._decisions.append(f"resample_policy=never — refusing incompatible rate/depth, forcing passthrough")
                self._passthrough = True

        if self._passthrough:
            self._decisions.append(f"PASSTHROUGH: {source_format} {sample_rate}Hz/{bit_depth}bit — no processing")
            logger.info(
                "pipeline_passthrough",
                format=source_format,
                sample_rate=sample_rate,
            )
            file_size = Path(file_path).stat().st_size if Path(file_path).exists() else 0
            self._stream_info = AudioStreamInfo(
                format=source_format,
                sample_rate=sample_rate,
                bit_depth=bit_depth,
                channels=channels,
                file_size=file_size,
            )
            self._pipeline_task = asyncio.create_task(
                self._passthrough_loop(file_path)
            )
        else:
            # --- Compute output parameters ---
            is_dsd = source_format == AudioFormat.DSD

            if is_dsd:
                # DSD: use 44.1kHz-family rate to avoid sample-rate conversion artefacts
                out_rate = _best_dsd_rate(self._capabilities.max_sample_rate)
                out_depth = min(24, self._capabilities.max_bit_depth)
                self._decisions.append(f"DSD→PCM: rate={out_rate}Hz (44.1kHz family), depth={out_depth}bit")
            else:
                out_rate = min(sample_rate, self._capabilities.max_sample_rate)
                out_depth = min(bit_depth, self._capabilities.max_bit_depth)

                # Integer ratio resampling preference
                if _settings.resample_policy == "integer_ratio" and out_rate != sample_rate:
                    integer_rates = [r for r in _441_FAMILY + [384000, 192000, 96000, 48000] if r <= self._capabilities.max_sample_rate and sample_rate % r == 0 or r % sample_rate == 0]
                    if integer_rates:
                        out_rate = max(r for r in integer_rates if r <= self._capabilities.max_sample_rate)
                        self._decisions.append(f"integer_ratio: {sample_rate}→{out_rate}Hz")

                if out_rate != sample_rate:
                    self._decisions.append(f"resample: {sample_rate}→{out_rate}Hz")
                if out_depth != bit_depth:
                    self._decisions.append(f"bit_depth: {bit_depth}→{out_depth}bit")

            # Ensure minimum 16-bit (DSD is 1-bit, FFmpeg outputs min s16le)
            if out_depth < 16:
                out_depth = 16

            # For URL sources, prefer FLAC over WAV if target supports it.
            use_flac = _is_url and AudioFormat.FLAC in self._capabilities.formats
            out_format = AudioFormat.FLAC if use_flac else AudioFormat.WAV
            self._decisions.append(f"TRANSCODE: {source_format}→{out_format} {out_rate}Hz/{out_depth}bit (flac_encode={use_flac})")

            logger.info(
                "pipeline_decode",
                source_format=source_format,
                source_rate=sample_rate,
                output_rate=out_rate,
                output_depth=out_depth,
                output_format=out_format,
            )

            self._stream_info = AudioStreamInfo(
                format=out_format,
                sample_rate=out_rate,
                bit_depth=out_depth,
                channels=channels,
            )

            # Decoder: source file → PCM or encoded format
            self._decoder = FFmpegDecoder(
                file_path=file_path,
                sample_rate=out_rate,
                bit_depth=out_depth,
                channels=channels,
                output_format=out_format if use_flac else None,
                icy_callback=self._icy_callback if _is_url else None,
                channel_filter=self._channel_filter,
                extra_filters=extra_filters,
            )
            await self._decoder.start(seek_ms=seek_ms)

            # Pipe decoder output directly to output buffer
            self._pipeline_task = asyncio.create_task(self._decode_loop())

        return self._stream_info

    async def _passthrough_loop(self, file_path: str) -> None:
        try:
            path = Path(file_path)
            hasher = hashlib.md5()
            with open(path, "rb") as f:
                while True:
                    chunk = await asyncio.to_thread(f.read, CHUNK_SIZE)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    await self._output_buffer.put(chunk)
            h = hasher.hexdigest()
            self._source_hash = h
            self._output_hash = h  # passthrough: identical
            logger.info("passthrough_checksum", hash=h, file=path.name)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("passthrough_error")
        finally:
            self._output_buffer.close()

    async def _decode_loop(self) -> None:
        """Pipe decoded audio from decoder to output buffer."""
        try:
            while True:
                chunk = await self._decoder.buffer.get()
                if chunk is None:
                    break
                await self._output_buffer.put(chunk)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("decode_loop_error")
        finally:
            self._output_buffer.close()

    async def stop(self) -> None:
        if self._pipeline_task:
            self._pipeline_task.cancel()
            try:
                await self._pipeline_task
            except asyncio.CancelledError:
                pass

        if self._decoder:
            await self._decoder.stop()

        self._output_buffer.close()
        self._output_buffer.reset()


# ---------------------------------------------------------------------------
# Rust-accelerated pipeline (Phase 2 migration)
# ---------------------------------------------------------------------------

try:
    import tune_native as _rust

    class RustAudioPipeline:
        """Drop-in replacement using tune_native.RustPipeline for FFmpeg."""

        def __init__(
            self,
            target_capabilities: AudioCapabilities,
            icy_callback=None,
            channel_filter: str | None = None,
        ) -> None:
            self._capabilities = target_capabilities
            self._icy_callback = icy_callback
            self._channel_filter = channel_filter
            self._rust_pipeline = None
            self._output_buffer = AsyncRingBuffer(max_chunks=512)
            self._feed_task: asyncio.Task | None = None
            self._passthrough = False
            self._stream_info: AudioStreamInfo | None = None
            self._decisions: list[str] = ["engine=rust"]

        @property
        def output_buffer(self) -> AsyncRingBuffer:
            return self._output_buffer

        @property
        def stream_info(self) -> AudioStreamInfo | None:
            return self._stream_info

        @property
        def is_passthrough(self) -> bool:
            return self._passthrough

        @property
        def decisions(self) -> list[str]:
            return self._decisions

        @property
        def source_hash(self) -> str | None:
            return None

        @property
        def output_hash(self) -> str | None:
            return None

        async def start(
            self,
            file_path: str,
            source_format: AudioFormat,
            sample_rate: int,
            bit_depth: int,
            channels: int,
            seek_ms: int = 0,
            extra_filters: str | None = None,
        ) -> AudioStreamInfo:
            _is_url = file_path.startswith("http://") or file_path.startswith("https://")
            self._passthrough = (
                can_passthrough(source_format, sample_rate, bit_depth, self._capabilities)
                and not _is_url
                and seek_ms == 0
                and not self._channel_filter
                and not extra_filters
            )

            if self._passthrough:
                file_size = Path(file_path).stat().st_size if Path(file_path).exists() else 0
                self._stream_info = AudioStreamInfo(
                    format=source_format,
                    sample_rate=sample_rate,
                    bit_depth=bit_depth,
                    channels=channels,
                    file_size=file_size,
                )
                self._decisions.append(f"PASSTHROUGH: {source_format}")
                return self._stream_info

            from tune_server.audio.formats import best_output_format
            out_fmt = best_output_format(source_format, sample_rate, bit_depth, self._capabilities)
            out_rate = min(sample_rate, self._capabilities.max_sample_rate)
            out_depth = min(bit_depth, self._capabilities.max_bit_depth)

            fmt_str = out_fmt.value if hasattr(out_fmt, 'value') else str(out_fmt)

            self._rust_pipeline = _rust.RustPipeline(
                file_path, fmt_str, out_rate, out_depth, channels,
                seek_ms if seek_ms > 0 else None, 512,
            )
            await asyncio.to_thread(self._rust_pipeline.start)

            self._stream_info = AudioStreamInfo(
                format=out_fmt,
                sample_rate=out_rate,
                bit_depth=out_depth,
                channels=channels,
            )
            self._decisions.append(f"RUST: {source_format}→{out_fmt} {out_rate}Hz/{out_depth}bit")

            self._feed_task = asyncio.create_task(self._feed_loop())
            return self._stream_info

        async def _feed_loop(self) -> None:
            try:
                while True:
                    chunk = await asyncio.to_thread(self._rust_pipeline.read_chunk, 5000)
                    if chunk is None:
                        break
                    await self._output_buffer.put(bytes(chunk))
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("rust_pipeline_feed_error")
            finally:
                self._output_buffer.close()

        async def stop(self) -> None:
            if self._feed_task:
                self._feed_task.cancel()
                try:
                    await self._feed_task
                except asyncio.CancelledError:
                    pass
            if self._rust_pipeline:
                await asyncio.to_thread(self._rust_pipeline.stop)
                self._rust_pipeline = None
            self._output_buffer.close()
            self._output_buffer.reset()

    _RUST_PIPELINE_AVAILABLE = True
    logger.info("rust_audio_pipeline_available")
except ImportError:
    _RUST_PIPELINE_AVAILABLE = False


def create_pipeline(
    target_capabilities: AudioCapabilities,
    icy_callback=None,
    channel_filter: str | None = None,
) -> AudioPipeline:
    """Factory: returns RustAudioPipeline if available, else Python."""
    import os
    engine = os.environ.get("TUNE_PIPELINE_ENGINE", "auto")
    use_rust = (
        _RUST_PIPELINE_AVAILABLE
        and engine != "python"
        and icy_callback is None  # Rust doesn't support ICY yet
        and channel_filter is None  # Rust doesn't support channel filter yet
    )
    if use_rust:
        return RustAudioPipeline(target_capabilities, icy_callback, channel_filter)
    return AudioPipeline(target_capabilities, icy_callback, channel_filter)

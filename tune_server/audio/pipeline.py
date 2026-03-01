from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from tune_server.audio.buffer import AsyncRingBuffer
from tune_server.audio.decoder import FFmpegDecoder
from tune_server.audio.formats import AudioCapabilities, can_passthrough
from tune_server.models import AudioFormat, AudioStreamInfo

logger = structlog.get_logger()

CHUNK_SIZE = 32768  # 32KB — matches decoder chunk size

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

    def __init__(self, target_capabilities: AudioCapabilities) -> None:
        self._capabilities = target_capabilities
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

    async def start(
        self,
        file_path: str,
        source_format: AudioFormat,
        sample_rate: int,
        bit_depth: int,
        channels: int,
        seek_ms: int = 0,
    ) -> AudioStreamInfo:
        # Check if we can do passthrough
        self._passthrough = can_passthrough(
            source_format, sample_rate, bit_depth, self._capabilities
        )

        _is_url = file_path.startswith("http://") or file_path.startswith("https://")
        if _is_url:
            self._passthrough = False

        if seek_ms > 0:
            self._passthrough = False

        if self._passthrough:
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
                # DSD is 1-bit; always decode to 24-bit for maximum dynamic range
                out_depth = min(24, self._capabilities.max_bit_depth)
            else:
                out_rate = min(sample_rate, self._capabilities.max_sample_rate)
                out_depth = min(bit_depth, self._capabilities.max_bit_depth)

            # Ensure minimum 16-bit (DSD is 1-bit, FFmpeg outputs min s16le)
            if out_depth < 16:
                out_depth = 16

            logger.info(
                "pipeline_decode",
                source_format=source_format,
                source_rate=sample_rate,
                output_rate=out_rate,
                output_depth=out_depth,
            )

            self._stream_info = AudioStreamInfo(
                format=AudioFormat.WAV,
                sample_rate=out_rate,
                bit_depth=out_depth,
                channels=channels,
            )

            # Decoder: source file → raw PCM (streamed as WAV with header)
            self._decoder = FFmpegDecoder(
                file_path=file_path,
                sample_rate=out_rate,
                bit_depth=out_depth,
                channels=channels,
            )
            await self._decoder.start(seek_ms=seek_ms)

            # Pipe decoder output directly to output buffer
            self._pipeline_task = asyncio.create_task(self._decode_loop())

        return self._stream_info

    async def _passthrough_loop(self, file_path: str) -> None:
        try:
            path = Path(file_path)
            # Stream file bytes directly
            with open(path, "rb") as f:
                while True:
                    chunk = await asyncio.to_thread(f.read, CHUNK_SIZE)
                    if not chunk:
                        break
                    await self._output_buffer.put(chunk)
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

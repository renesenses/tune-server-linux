from __future__ import annotations

import asyncio

import structlog

from tune_server.audio.buffer import AsyncRingBuffer
from tune_server.audio.formats import ffmpeg_codec_arg, ffmpeg_format_arg
from tune_server.config import settings
from tune_server.models import AudioFormat

logger = structlog.get_logger()

CHUNK_SIZE = 4096


class FFmpegEncoder:
    """Encode raw PCM to a target format via FFmpeg subprocess."""

    def __init__(
        self,
        output_format: AudioFormat,
        sample_rate: int = 44100,
        bit_depth: int = 16,
        channels: int = 2,
    ) -> None:
        self._output_format = output_format
        self._sample_rate = sample_rate
        self._bit_depth = bit_depth
        self._channels = channels
        self._process: asyncio.subprocess.Process | None = None
        self._output_buffer = AsyncRingBuffer(max_chunks=128)
        self._read_task: asyncio.Task | None = None

    @property
    def output_buffer(self) -> AsyncRingBuffer:
        return self._output_buffer

    def _pcm_format(self) -> str:
        if self._bit_depth <= 16:
            return "s16le"
        elif self._bit_depth <= 24:
            return "s24le"
        else:
            return "s32le"

    async def start(self) -> None:
        codec = ffmpeg_codec_arg(self._output_format)
        fmt = ffmpeg_format_arg(self._output_format)

        cmd = [
            settings.ffmpeg_path,
            "-hide_banner", "-loglevel", "error",
            "-f", self._pcm_format(),
            "-ar", str(self._sample_rate),
            "-ac", str(self._channels),
            "-i", "pipe:0",
            "-acodec", codec,
            "-f", fmt,
            "pipe:1",
        ]

        logger.debug("ffmpeg_encode_start", format=self._output_format, cmd=" ".join(cmd))

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._read_task = asyncio.create_task(self._read_output())

    async def _read_output(self) -> None:
        try:
            while True:
                chunk = await self._process.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break
                await self._output_buffer.put(chunk)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("encoder_read_error")
        finally:
            self._output_buffer.close()

    async def write(self, pcm_data: bytes) -> None:
        if self._process and self._process.stdin:
            self._process.stdin.write(pcm_data)
            await self._process.stdin.drain()

    async def finish(self) -> None:
        if self._process and self._process.stdin:
            self._process.stdin.close()
            await self._process.stdin.wait_closed()

    async def stop(self) -> None:
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass

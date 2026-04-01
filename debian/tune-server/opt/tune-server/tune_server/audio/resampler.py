from __future__ import annotations

import asyncio

import structlog

from tune_server.config import settings

logger = structlog.get_logger()

CHUNK_SIZE = 4096


class Resampler:
    """Resample audio using FFmpeg's soxr resampler. Never upsamples."""

    def __init__(
        self,
        source_rate: int,
        target_rate: int,
        source_depth: int = 16,
        target_depth: int = 16,
        channels: int = 2,
    ) -> None:
        self._source_rate = source_rate
        self._target_rate = min(target_rate, source_rate)  # Never upsample
        self._source_depth = source_depth
        self._target_depth = min(target_depth, source_depth)
        self._channels = channels
        self._process: asyncio.subprocess.Process | None = None
        self._needs_resample = (
            self._source_rate != self._target_rate
            or self._source_depth != self._target_depth
        )

    @property
    def needs_resample(self) -> bool:
        return self._needs_resample

    @property
    def output_rate(self) -> int:
        return self._target_rate

    @property
    def output_depth(self) -> int:
        return self._target_depth

    def _pcm_format(self, depth: int) -> str:
        if depth <= 16:
            return "s16le"
        elif depth <= 24:
            return "s24le"
        else:
            return "s32le"

    async def start(self) -> None:
        if not self._needs_resample:
            return

        in_fmt = self._pcm_format(self._source_depth)
        out_fmt = self._pcm_format(self._target_depth)

        cmd = [
            settings.ffmpeg_path,
            "-hide_banner", "-loglevel", "error",
            "-f", in_fmt,
            "-ar", str(self._source_rate),
            "-ac", str(self._channels),
            "-i", "pipe:0",
            "-af", f"aresample=resampler=soxr:out_sample_rate={self._target_rate}",
            "-f", out_fmt,
            "-ac", str(self._channels),
            "pipe:1",
        ]

        logger.debug(
            "resampler_start",
            source_rate=self._source_rate,
            target_rate=self._target_rate,
        )

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def process(self, pcm_data: bytes) -> bytes:
        if not self._needs_resample:
            return pcm_data

        if not self._process:
            raise RuntimeError("Resampler not started")

        self._process.stdin.write(pcm_data)
        await self._process.stdin.drain()

        # Read available output
        return await self._process.stdout.read(len(pcm_data))

    async def finish(self) -> bytes:
        if not self._needs_resample or not self._process:
            return b""

        self._process.stdin.close()
        await self._process.stdin.wait_closed()
        remaining = await self._process.stdout.read()
        return remaining

    async def stop(self) -> None:
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass

from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from tune_server.audio.buffer import AsyncRingBuffer
from tune_server.config import settings
from tune_server.models import AudioFormat

logger = structlog.get_logger()

# 32KB chunks — larger buffers reduce context-switch overhead for hi-res streams
CHUNK_SIZE = 32768


class FFmpegDecoder:
    """Decode/transcode audio files via FFmpeg subprocess.

    When output_format is None (default), outputs raw PCM.
    When output_format is set (e.g. FLAC), FFmpeg encodes to that format.
    """

    def __init__(
        self,
        file_path: str,
        sample_rate: int = 44100,
        bit_depth: int = 16,
        channels: int = 2,
        output_format: Optional[AudioFormat] = None,
    ) -> None:
        self._file_path = file_path
        self._sample_rate = sample_rate
        self._bit_depth = bit_depth
        self._channels = channels
        self._output_format = output_format
        self._process: asyncio.subprocess.Process | None = None
        self._buffer = AsyncRingBuffer(max_chunks=512)
        self._read_task: asyncio.Task | None = None

    @property
    def buffer(self) -> AsyncRingBuffer:
        return self._buffer

    def _pcm_format(self) -> str:
        if self._bit_depth <= 16:
            return "s16le"
        elif self._bit_depth <= 24:
            return "s24le"
        else:
            return "s32le"

    def _build_cmd(self, seek_ms: int = 0) -> list[str]:
        cmd = [settings.ffmpeg_path, "-hide_banner", "-loglevel", "error"]

        if seek_ms > 0:
            seconds = seek_ms / 1000.0
            cmd.extend(["-ss", f"{seconds:.3f}"])

        cmd.extend(["-i", self._file_path])

        if self._output_format == AudioFormat.FLAC:
            # Transcode to FLAC — lossless, low latency
            # FLAC encoder only supports s16 and s32; use s32 for >16-bit
            sample_fmt = "s16" if self._bit_depth <= 16 else "s32"
            cmd.extend([
                "-f", "flac",
                "-ar", str(self._sample_rate),
                "-ac", str(self._channels),
                "-sample_fmt", sample_fmt,
                "-compression_level", "0",  # fastest encoding
                "pipe:1",
            ])
        else:
            # Raw PCM output
            pcm = self._pcm_format()
            cmd.extend([
                "-f", pcm,
                "-ar", str(self._sample_rate),
                "-ac", str(self._channels),
                "-acodec", f"pcm_{pcm}",
                "pipe:1",
            ])

        return cmd

    async def start(self, seek_ms: int = 0) -> None:
        cmd = self._build_cmd(seek_ms)

        logger.debug("ffmpeg_decode_start", file=self._file_path, cmd=" ".join(cmd))

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
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
                await self._buffer.put(chunk)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("ffmpeg_read_error")
        finally:
            self._buffer.close()

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

    async def wait(self) -> int:
        if self._read_task:
            await self._read_task
        if self._process:
            return await self._process.wait()
        return -1


class FFmpegProbe:
    """Probe audio file info with ffprobe."""

    @staticmethod
    async def probe(file_path: str) -> dict | None:
        cmd = [
            settings.ffprobe_path,
            "-hide_banner", "-loglevel", "error",
            "-show_format", "-show_streams",
            "-select_streams", "a:0",
            "-print_format", "json",
            file_path,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error("ffprobe_error", stderr=stderr.decode())
                return None

            import json
            return json.loads(stdout.decode())
        except Exception:
            logger.exception("ffprobe_exception", file=file_path)
            return None

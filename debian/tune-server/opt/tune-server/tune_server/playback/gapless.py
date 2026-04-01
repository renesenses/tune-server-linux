from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from tune_server.audio.formats import AudioCapabilities
from tune_server.audio.pipeline import AudioPipeline
from tune_server.models import AudioFormat, AudioStreamInfo, Track

logger = structlog.get_logger()


class GaplessHandler:
    """Pre-decode the next track for gapless transitions."""

    def __init__(self, capabilities: AudioCapabilities) -> None:
        self._capabilities = capabilities
        self._next_pipeline: AudioPipeline | None = None
        self._next_track: Track | None = None
        self._next_stream_info: AudioStreamInfo | None = None
        self._preload_task: asyncio.Task | None = None

    @property
    def has_next(self) -> bool:
        return self._next_pipeline is not None

    @property
    def next_track(self) -> Optional[Track]:
        return self._next_track

    @property
    def next_stream_info(self) -> Optional[AudioStreamInfo]:
        return self._next_stream_info

    async def preload(self, track: Track) -> None:
        """Start pre-decoding the next track."""
        await self.cancel()

        self._next_track = track
        self._preload_task = asyncio.create_task(self._do_preload(track))

    async def _do_preload(self, track: Track) -> None:
        try:
            source_format = AudioFormat(track.format) if track.format else AudioFormat.FLAC
            pipeline = AudioPipeline(self._capabilities)
            stream_info = await pipeline.start(
                file_path=track.file_path,
                source_format=source_format,
                sample_rate=track.sample_rate or 44100,
                bit_depth=track.bit_depth or 16,
                channels=track.channels or 2,
            )
            self._next_pipeline = pipeline
            self._next_stream_info = stream_info
            logger.info("gapless_preloaded", track=track.title)
        except Exception:
            logger.exception("gapless_preload_error", track=track.title)
            self._next_pipeline = None

    def take_pipeline(self) -> tuple[Optional[AudioPipeline], Optional[AudioStreamInfo]]:
        """Take ownership of the preloaded pipeline."""
        pipeline = self._next_pipeline
        info = self._next_stream_info
        self._next_pipeline = None
        self._next_stream_info = None
        self._next_track = None
        return pipeline, info

    async def cancel(self) -> None:
        if self._preload_task:
            self._preload_task.cancel()
            try:
                await self._preload_task
            except asyncio.CancelledError:
                pass
            self._preload_task = None

        if self._next_pipeline:
            await self._next_pipeline.stop()
            self._next_pipeline = None

        self._next_track = None
        self._next_stream_info = None

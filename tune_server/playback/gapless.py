from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from tune_server.audio.formats import AudioCapabilities
from tune_server.audio.pipeline import AudioPipeline
from tune_server.models import AudioFormat, AudioStreamInfo, Track

logger = structlog.get_logger()


class GaplessHandler:
    """Pre-decode the next track for gapless transitions.

    Supports early pre-buffering: when the current track is within
    PRE_BUFFER_THRESHOLD_MS of finishing, the next track's first chunks
    are decoded and cached so the switch is instantaneous.
    """

    PRE_BUFFER_THRESHOLD_MS = 10_000  # start pre-buffering 10s before end

    def __init__(self, capabilities: AudioCapabilities) -> None:
        self._capabilities = capabilities
        self._next_pipeline: AudioPipeline | None = None
        self._next_track: Track | None = None
        self._next_stream_info: AudioStreamInfo | None = None
        self._preload_task: asyncio.Task | None = None
        self._pre_buffered_chunks: list[bytes] = []
        self._pre_buffer_done: bool = False

    @property
    def has_next(self) -> bool:
        return self._next_pipeline is not None

    @property
    def next_track(self) -> Optional[Track]:
        return self._next_track

    @property
    def next_stream_info(self) -> Optional[AudioStreamInfo]:
        return self._next_stream_info

    @property
    def pre_buffered_chunks(self) -> list[bytes]:
        return self._pre_buffered_chunks

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

            # Pre-buffer the first few chunks so they are ready instantly
            self._pre_buffered_chunks = []
            self._pre_buffer_done = False
            max_pre_buffer_chunks = 10  # ~first few seconds
            if pipeline.output_buffer:
                for _ in range(max_pre_buffer_chunks):
                    try:
                        chunk = await asyncio.wait_for(
                            pipeline.output_buffer.get(), timeout=2.0
                        )
                        if chunk is None:
                            self._pre_buffer_done = True
                            break
                        self._pre_buffered_chunks.append(chunk)
                    except asyncio.TimeoutError:
                        break

            logger.info("gapless_preloaded", track=track.title,
                        pre_buffered_chunks=len(self._pre_buffered_chunks))
        except Exception:
            logger.exception("gapless_preload_error", track=track.title)
            self._next_pipeline = None
            self._pre_buffered_chunks = []

    def take_pipeline(self) -> tuple[Optional[AudioPipeline], Optional[AudioStreamInfo], list[bytes]]:
        """Take ownership of the preloaded pipeline and pre-buffered chunks."""
        pipeline = self._next_pipeline
        info = self._next_stream_info
        chunks = self._pre_buffered_chunks
        self._next_pipeline = None
        self._next_stream_info = None
        self._next_track = None
        self._pre_buffered_chunks = []
        self._pre_buffer_done = False
        return pipeline, info, chunks

    async def cancel(self) -> None:
        if self._preload_task:
            self._preload_task.cancel()
            try:
                await self._preload_task
            except asyncio.CancelledError:
                logger.debug("gapless_preload_cancelled")
            self._preload_task = None

        if self._next_pipeline:
            await self._next_pipeline.stop()
            self._next_pipeline = None

        self._next_track = None
        self._next_stream_info = None
        self._pre_buffered_chunks = []
        self._pre_buffer_done = False

    def pre_buffer_track(self, track: Track) -> None:
        """Schedule pre-buffering for a track (called early, e.g. 10s before end).

        If already preloading the same track, this is a no-op.
        Otherwise it kicks off a new preload.
        """
        if self._next_track and self._next_track.id == track.id:
            return  # already preloading this track
        # Fire-and-forget preload
        asyncio.ensure_future(self.preload(track))

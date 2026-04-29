"""Snapcast output target.

Each Tune zone of OutputType.SNAPCAST owns a per-zone PCM FIFO that
snapserver consumes as a `pipe` source. Volume + group membership is
controlled via snapserver's JSON-RPC. Time alignment across endpoints
is delivered by Snapcast itself (~50 ms, FLAC-compressed on the wire).

DSD/passthrough is impossible: snapcast playout is fundamentally
clock-aligned PCM. Hi-res sources are resampled by the existing
audio pipeline to s16le @ 44.1 / 48 / 88.2 / 96 kHz before reaching
this output. See SNAPCAST_CAPABILITIES in tune_server.audio.formats.

Skeleton — runtime not implemented yet (v0.8.0 milestone task #45).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import structlog

from tune_server.audio.formats import SNAPCAST_CAPABILITIES, AudioCapabilities
from tune_server.models import AudioStreamInfo, Track
from tune_server.outputs.base import OutputTarget

if TYPE_CHECKING:
    from tune_server.zones.snapcast_manager import SnapcastManager

logger = structlog.get_logger()


class SnapcastOutput(OutputTarget):
    """Per-zone snapcast stream. Backed by a named FIFO that snapserver
    reads, and JSON-RPC commands for volume/grouping/control."""

    def __init__(
        self,
        manager: "SnapcastManager",
        stream_name: str,
        fifo_path: Path,
        client_ids: list[str] | None = None,
    ) -> None:
        self._manager = manager
        self._stream_name = stream_name
        self._fifo_path = fifo_path
        self._client_ids: list[str] = list(client_ids or [])
        self._fifo_fd: int | None = None

    @property
    def name(self) -> str:
        return f"Snapcast: {self._stream_name}"

    @property
    def capabilities(self) -> AudioCapabilities:
        return SNAPCAST_CAPABILITIES

    @property
    def is_available(self) -> bool:
        # The FIFO is created by SnapcastManager.register_stream; missing
        # FIFO = manager not running or stream not registered.
        return self._fifo_path.exists()

    async def start(self, stream_info: AudioStreamInfo, track: Optional[Track] = None) -> None:
        # Open the per-zone FIFO non-blocking for write. NONBLOCK is
        # critical: without it, opening blocks until a reader is
        # connected — and snapserver only opens its read side when a
        # client subscribes to the stream. The write end keeps blocking
        # behaviour for the actual writes (we want backpressure when
        # the buffer is full, just not on open).
        if self._fifo_fd is None:
            try:
                self._fifo_fd = os.open(
                    self._fifo_path, os.O_WRONLY | os.O_NONBLOCK,
                )
            except OSError as exc:
                # ENXIO = no reader yet (snapserver not running, or no
                # client subscribed). Reopen lazily on first write.
                logger.debug(
                    "snapcast_fifo_open_deferred",
                    fifo=str(self._fifo_path), errno=exc.errno,
                )
                self._fifo_fd = None
        # Make sure the snapcast group for this stream contains our
        # bound clients (when there are any). No clients = stream stays
        # idle on the snapcast side, audio is consumed and discarded by
        # snapserver, no big deal.
        if self._client_ids:
            await self._manager.set_clients_for_stream(
                self._stream_name, self._client_ids,
            )

    async def write(self, data: bytes) -> None:
        if self._fifo_fd is None:
            # Try to open lazily — a snapclient may have subscribed
            # since `start()`.
            try:
                self._fifo_fd = os.open(
                    self._fifo_path, os.O_WRONLY | os.O_NONBLOCK,
                )
            except OSError:
                # Still no reader. Drop the buffer silently — better
                # than crashing the pipeline. Snapcast users see ~1 s
                # of silence then audio kicks in once a client
                # subscribes.
                return
        try:
            os.write(self._fifo_fd, data)
        except BrokenPipeError:
            # Reader went away (snapclient disconnect). Close and let
            # the next write retry the open.
            try:
                os.close(self._fifo_fd)
            except OSError:
                pass
            self._fifo_fd = None
        except BlockingIOError:
            # Snapserver buffer full — drop. Snapcast already buffers
            # ~1 s on the client side; dropping a chunk here is less
            # bad than blocking the whole pipeline.
            logger.debug("snapcast_fifo_full_drop", stream=self._stream_name)

    async def flush(self) -> None:
        return None

    async def pause(self) -> None:
        # Snapcast has no native pause — pause = stop the upstream
        # pipeline. Snapclients hear ~1 s of silence then idle.
        return None

    async def resume(self) -> None:
        return None

    async def stop(self) -> None:
        if self._fifo_fd is not None:
            try:
                os.close(self._fifo_fd)
            except OSError:
                pass
            self._fifo_fd = None

    async def set_volume(self, volume: float) -> None:
        # Tune passes 0.0-1.0; snapcast is 0-100.
        v = max(0, min(100, int(volume * 100)))
        for client_id in self._client_ids:
            await self._manager.set_client_volume(client_id, v)

    async def close(self) -> None:
        await self.stop()

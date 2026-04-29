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
        # Skeleton: open FIFO non-blocking for write, sync clients to
        # snapcast group via JSON-RPC. Real implementation lands in #45
        # follow-up.
        raise NotImplementedError("SnapcastOutput.start — v0.8.0 task #45")

    async def write(self, data: bytes) -> None:
        raise NotImplementedError("SnapcastOutput.write — v0.8.0 task #45")

    async def flush(self) -> None:
        return None

    async def pause(self) -> None:
        # Snapcast has no native pause — pause = stop the upstream
        # pipeline. Snapclients hear ~1 s of silence then idle.
        return None

    async def resume(self) -> None:
        return None

    async def stop(self) -> None:
        raise NotImplementedError("SnapcastOutput.stop — v0.8.0 task #45")

    async def set_volume(self, volume: float) -> None:
        # JSON-RPC: per-client `Client.SetVolume`, or the snapcast group
        # if multiple clients. Volume is 0-100 in snapcast.
        raise NotImplementedError("SnapcastOutput.set_volume — v0.8.0 task #45")

    async def close(self) -> None:
        if self._fifo_fd is not None:
            try:
                os.close(self._fifo_fd)
            except OSError:
                pass
            self._fifo_fd = None

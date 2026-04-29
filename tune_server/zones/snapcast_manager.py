"""Snapcast lifecycle manager.

Owns the snapserver child process, generates snapserver.conf on-demand
as zones come and go, and exposes a small JSON-RPC client for runtime
control (Server.GetStatus, Group.SetClients, Client.SetVolume,
Stream.AddStream).

One snapserver per Tune Server instance. Per-zone streams (each Tune
zone of OutputType.SNAPCAST = one `[stream]` block in snapserver.conf
+ one named FIFO under `${runtime_dir}/snapfifo-{zone_id}`).

Linux + macOS only. Windows falls back to DLNA/AirPlay — the factory
in `tune_server.zones.manager` is gated on `sys.platform`.

Skeleton — runtime not implemented yet (v0.8.0 milestone task #45).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()


SNAPSERVER_DEFAULT_HTTP_PORT = 1780
SNAPSERVER_DEFAULT_AUDIO_PORT = 1704
SNAPSERVER_DEFAULT_RPC_PORT = 1705


@dataclass(frozen=True)
class SnapcastClient:
    """A client/endpoint connected to our snapserver, as reported by
    `Server.GetStatus`. UUIDs are assigned by snapserver on first
    connection and persist across reconnects."""

    id: str
    name: str
    host: str
    mac: str | None
    connected: bool
    volume: int  # 0–100


class SnapcastManager:
    """Owns the snapserver subprocess + JSON-RPC client.

    Lifecycle:
      - `start()` : locate snapserver binary, write initial conf, spawn,
                    open JSON-RPC over WebSocket to localhost.
      - `register_stream(zone_id, sample_rate, bit_depth)` -> (stream_name, fifo_path)
      - `unregister_stream(zone_id)` : remove from conf, restart.
      - `set_clients_for_stream(stream_name, client_ids)` : JSON-RPC.
      - `list_clients()` : push-driven cache from snapserver notifications.
      - `stop()` : SIGTERM with 2 s grace then SIGKILL.

    HUP-vs-restart trade-off : SIGHUP picks up `[client]` and volume
    edits but not `[stream]` source changes. Stream add/remove
    therefore triggers a full restart (~500 ms gap on currently-
    playing zones). Mitigated by debouncing batch zone CRUD 250 ms.
    """

    def __init__(self, runtime_dir: Path, binary: Optional[Path] = None) -> None:
        self._runtime_dir = runtime_dir
        self._binary = binary
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._streams: dict[int, str] = {}  # zone_id -> stream_name
        self._clients: dict[str, SnapcastClient] = {}
        self._rpc_task: Optional[asyncio.Task] = None

    @property
    def is_supported(self) -> bool:
        """Snapcast server is reliably built only for Linux and macOS."""
        return sys.platform in ("linux", "darwin")

    @property
    def binary_path(self) -> Optional[Path]:
        if self._binary and self._binary.is_file():
            return self._binary
        which = shutil.which("snapserver")
        return Path(which) if which else None

    async def start(self) -> None:
        if not self.is_supported:
            logger.info("snapcast_skipped_platform", platform=sys.platform)
            return
        if self.binary_path is None:
            logger.warning("snapcast_binary_missing — install snapserver to enable Snapcast zones")
            return
        # TODO #45: write initial conf, spawn, open JSON-RPC, hydrate clients.
        logger.info("snapcast_manager_start_skeleton", binary=str(self.binary_path))

    async def stop(self) -> None:
        if self._rpc_task is not None:
            self._rpc_task.cancel()
            self._rpc_task = None
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
            self._proc = None

    # --- stream registration ------------------------------------------

    async def register_stream(
        self, zone_id: int, sample_rate: int, bit_depth: int,
    ) -> tuple[str, Path]:
        """Register a per-zone stream. Returns (stream_name, fifo_path)."""
        stream_name = f"tune-zone-{zone_id}"
        fifo_path = self._runtime_dir / f"snapfifo-{zone_id}"
        if not fifo_path.exists():
            os.mkfifo(fifo_path, mode=0o666)
        self._streams[zone_id] = stream_name
        # TODO #45: rewrite snapserver.conf, restart, debounced.
        logger.info(
            "snapcast_stream_registered_skeleton",
            zone_id=zone_id, stream=stream_name, fifo=str(fifo_path),
            sample_rate=sample_rate, bit_depth=bit_depth,
        )
        return stream_name, fifo_path

    async def unregister_stream(self, zone_id: int) -> None:
        stream_name = self._streams.pop(zone_id, None)
        if stream_name is None:
            return
        fifo_path = self._runtime_dir / f"snapfifo-{zone_id}"
        try:
            fifo_path.unlink(missing_ok=True)
        except OSError:
            pass
        # TODO #45: rewrite conf + restart.
        logger.info("snapcast_stream_unregistered_skeleton", zone_id=zone_id)

    # --- runtime control ----------------------------------------------

    async def set_clients_for_stream(
        self, stream_name: str, client_ids: list[str],
    ) -> None:
        """Reassign which snapclients receive which stream."""
        # TODO #45: JSON-RPC `Group.SetClients`.
        logger.debug(
            "snapcast_set_clients_skeleton",
            stream=stream_name, clients=client_ids,
        )

    async def list_clients(self) -> list[SnapcastClient]:
        """All snapclients ever seen by snapserver, with current
        connected/volume state. Push-updated from JSON-RPC notifications;
        falls back to a `Server.GetStatus` poll on bootstrap."""
        return list(self._clients.values())

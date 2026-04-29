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
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiohttp
import structlog

logger = structlog.get_logger()


SNAPSERVER_DEFAULT_HTTP_PORT = 1780
SNAPSERVER_DEFAULT_AUDIO_PORT = 1704
SNAPSERVER_DEFAULT_RPC_PORT = 1705

# Debounce window for snapserver.conf rewrites — multiple zone CRUD ops
# happening close together (e.g. an onboarding wizard creating four
# zones in a row) collapse to a single config rewrite + restart.
_RESTART_DEBOUNCE_S = 0.25


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

    def __init__(
        self,
        runtime_dir: Path,
        binary: Optional[Path] = None,
        http_port: int = SNAPSERVER_DEFAULT_HTTP_PORT,
    ) -> None:
        self._runtime_dir = runtime_dir
        self._binary = binary
        self._http_port = http_port
        self._proc: Optional[asyncio.subprocess.Process] = None
        # zone_id -> {stream_name, sample_rate, bit_depth}
        self._streams: dict[int, dict[str, Any]] = {}
        self._clients: dict[str, SnapcastClient] = {}
        self._restart_pending: Optional[asyncio.Task] = None
        self._rpc_session: Optional[aiohttp.ClientSession] = None
        self._rpc_id: int = 0

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
            logger.warning(
                "snapcast_binary_missing — install snapserver to enable Snapcast zones"
            )
            return
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        self._write_config()
        await self._spawn()
        # Hydrate the client cache once. WebSocket-driven push notifications
        # are a v0.8.x follow-up; for now we just poll on demand from
        # `list_clients()`.
        self._rpc_session = aiohttp.ClientSession()
        await self._refresh_clients()
        logger.info(
            "snapcast_manager_started",
            binary=str(self.binary_path),
            http_port=self._http_port,
            streams=len(self._streams),
        )

    async def stop(self) -> None:
        if self._restart_pending is not None:
            self._restart_pending.cancel()
            self._restart_pending = None
        if self._rpc_session is not None:
            await self._rpc_session.close()
            self._rpc_session = None
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None

    # --- config + process lifecycle -----------------------------------

    @property
    def config_path(self) -> Path:
        return self._runtime_dir / "snapserver.conf"

    def _write_config(self) -> None:
        """Render the snapserver.conf file from the current streams dict.

        We only emit blocks we actually need — `[server]`, `[http]`,
        `[tcp]`, and one `[stream]` source = pipe per registered zone.
        Snapserver picks the first stream as default, but each Tune
        client targets its zone's stream explicitly via
        `Group.SetClients`, so default-stream choice doesn't matter.
        """
        lines: list[str] = [
            "# Auto-generated by tune_server.zones.snapcast_manager",
            "# Do not edit by hand — overwritten on every zone CRUD.",
            "[server]",
            f"datadir = {self._runtime_dir}",
            "",
            "[http]",
            "enabled = true",
            "doc_root = ",
            "bind_to_address = 0.0.0.0",
            f"port = {self._http_port}",
            "",
            "[tcp]",
            "enabled = true",
            "bind_to_address = 0.0.0.0",
            f"port = {SNAPSERVER_DEFAULT_AUDIO_PORT}",
            "",
            "[logging]",
            "filter = *:info",
            "",
        ]
        if not self._streams:
            # snapserver requires at least one stream — write a silent
            # placeholder. Removed as soon as a real zone is registered.
            placeholder_fifo = self._runtime_dir / "snapfifo-placeholder"
            if not placeholder_fifo.exists():
                try:
                    os.mkfifo(placeholder_fifo, 0o666)
                except FileExistsError:
                    pass
            lines += [
                "[stream]",
                f"source = pipe://{placeholder_fifo}?name=tune-placeholder"
                "&sampleformat=44100:16:2",
                "",
            ]
        for zone_id, info in sorted(self._streams.items()):
            fifo_path = self._runtime_dir / f"snapfifo-{zone_id}"
            sr = info["sample_rate"]
            bd = info["bit_depth"]
            stream_name = info["stream_name"]
            lines += [
                "[stream]",
                f"source = pipe://{fifo_path}?name={stream_name}"
                f"&sampleformat={sr}:{bd}:2",
                "",
            ]
        self.config_path.write_text("\n".join(lines), encoding="utf-8")

    async def _spawn(self) -> None:
        """Spawn snapserver as a child process."""
        assert self.binary_path is not None
        self._proc = await asyncio.create_subprocess_exec(
            str(self.binary_path),
            "--config", str(self.config_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Give snapserver a beat to bind its sockets before the first
        # JSON-RPC call. 200 ms is empirically enough on macOS / Linux.
        await asyncio.sleep(0.2)
        if self._proc.returncode is not None:
            stderr = await self._proc.stderr.read() if self._proc.stderr else b""
            logger.error(
                "snapserver_died_at_spawn",
                rc=self._proc.returncode,
                stderr=stderr.decode("utf-8", errors="replace")[:500],
            )

    async def _restart_after_debounce(self) -> None:
        """Coalesce multiple register_stream/unregister_stream calls into
        a single config-rewrite + restart. Snapserver only re-reads
        `[client]` on SIGHUP, not `[stream]` source changes — so any
        stream CRUD requires a real restart (~500 ms gap)."""
        try:
            await asyncio.sleep(_RESTART_DEBOUNCE_S)
        except asyncio.CancelledError:
            return
        self._restart_pending = None
        if self._proc is None:
            return
        self._write_config()
        # Graceful TERM, fall through to KILL on timeout.
        if self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        await self._spawn()
        await self._refresh_clients()

    def _schedule_restart(self) -> None:
        """Trigger a debounced restart. Cheap to call repeatedly —
        the previous task is cancelled and replaced, so a burst of
        zone CRUD ops collapses to one restart."""
        if self._restart_pending is not None and not self._restart_pending.done():
            self._restart_pending.cancel()
        self._restart_pending = asyncio.create_task(self._restart_after_debounce())

    # --- JSON-RPC -----------------------------------------------------

    async def _rpc(self, method: str, params: dict | None = None) -> Any:
        """Call snapserver's JSON-RPC endpoint over HTTP. Returns the
        `result` payload. Raises on transport / RPC errors. WebSocket
        push notifications are a v0.8.x follow-up — for now we poll."""
        if self._rpc_session is None:
            raise RuntimeError("snapcast_rpc_session_not_started")
        self._rpc_id += 1
        payload = {
            "id": self._rpc_id,
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        url = f"http://127.0.0.1:{self._http_port}/jsonrpc"
        async with self._rpc_session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            data = await resp.json()
        if "error" in data:
            raise RuntimeError(f"snapcast_rpc_error: {data['error']}")
        return data.get("result")

    async def _refresh_clients(self) -> None:
        """Pull the current client list from snapserver."""
        if self._rpc_session is None:
            return
        try:
            status = await self._rpc("Server.GetStatus")
        except Exception as exc:
            logger.debug("snapcast_get_status_failed", error=repr(exc))
            return
        # Schema: result.server.groups[].clients[]
        clients: dict[str, SnapcastClient] = {}
        for group in (status or {}).get("server", {}).get("groups", []):
            for c in group.get("clients", []):
                cid = c.get("id", "")
                if not cid:
                    continue
                clients[cid] = SnapcastClient(
                    id=cid,
                    name=(c.get("config") or {}).get("name") or c.get("host", {}).get("name", cid),
                    host=c.get("host", {}).get("name", ""),
                    mac=c.get("host", {}).get("mac"),
                    connected=bool(c.get("connected", False)),
                    volume=int((c.get("config") or {}).get("volume", {}).get("percent", 100)),
                )
        self._clients = clients

    # --- stream registration ------------------------------------------

    async def register_stream(
        self, zone_id: int, sample_rate: int, bit_depth: int,
    ) -> tuple[str, Path]:
        """Register a per-zone stream. Returns (stream_name, fifo_path).
        Triggers a debounced snapserver restart so the new `[stream]`
        block is picked up — SIGHUP doesn't suffice for stream config."""
        stream_name = f"tune-zone-{zone_id}"
        fifo_path = self._runtime_dir / f"snapfifo-{zone_id}"
        if not fifo_path.exists():
            os.mkfifo(fifo_path, mode=0o666)
        self._streams[zone_id] = {
            "stream_name": stream_name,
            "sample_rate": sample_rate,
            "bit_depth": bit_depth,
        }
        logger.info(
            "snapcast_stream_registered",
            zone_id=zone_id, stream=stream_name, fifo=str(fifo_path),
            sample_rate=sample_rate, bit_depth=bit_depth,
        )
        self._schedule_restart()
        return stream_name, fifo_path

    async def unregister_stream(self, zone_id: int) -> None:
        info = self._streams.pop(zone_id, None)
        if info is None:
            return
        fifo_path = self._runtime_dir / f"snapfifo-{zone_id}"
        try:
            fifo_path.unlink(missing_ok=True)
        except OSError:
            pass
        logger.info("snapcast_stream_unregistered", zone_id=zone_id)
        self._schedule_restart()

    # --- runtime control ----------------------------------------------

    async def set_clients_for_stream(
        self, stream_name: str, client_ids: list[str],
    ) -> None:
        """Reassign which snapclients receive which stream.

        Snapserver groups clients per stream — moving a client between
        streams = removing it from its current group and adding to the
        target group. We resolve the target group via `Server.GetStatus`
        and then call `Group.SetClients`.
        """
        if not client_ids:
            return
        try:
            status = await self._rpc("Server.GetStatus")
        except Exception as exc:
            logger.warning("snapcast_set_clients_status_failed", error=repr(exc))
            return
        groups = (status or {}).get("server", {}).get("groups", [])
        target_group_id: Optional[str] = None
        for g in groups:
            if (g.get("stream_id") or "") == stream_name:
                target_group_id = g.get("id")
                break
        if target_group_id is None and groups:
            # No group for this stream yet — borrow the first group and
            # repoint it. Snapserver will create or merge as needed.
            target_group_id = groups[0].get("id")
        if target_group_id is None:
            logger.warning("snapcast_no_groups_available", stream=stream_name)
            return
        try:
            await self._rpc(
                "Group.SetClients",
                {"id": target_group_id, "clients": client_ids},
            )
            await self._rpc(
                "Group.SetStream",
                {"id": target_group_id, "stream_id": stream_name},
            )
        except Exception as exc:
            logger.warning(
                "snapcast_set_clients_failed",
                stream=stream_name, error=repr(exc),
            )

    async def set_client_volume(self, client_id: str, volume: int) -> None:
        """Set per-client volume (0–100)."""
        volume = max(0, min(100, int(volume)))
        try:
            await self._rpc(
                "Client.SetVolume",
                {"id": client_id, "volume": {"muted": volume == 0, "percent": volume}},
            )
        except Exception as exc:
            logger.warning(
                "snapcast_set_volume_failed",
                client_id=client_id, error=repr(exc),
            )

    async def list_clients(self) -> list[SnapcastClient]:
        """All snapclients ever seen by snapserver, with current
        connected/volume state. Refreshed on each call (~50 ms over
        loopback HTTP). WebSocket push subscription is a v0.8.x
        follow-up."""
        await self._refresh_clients()
        return list(self._clients.values())

"""Tests for the Snapcast manager + output (v0.8.0).

We can't run a real snapserver in CI — these tests mock the subprocess
and aiohttp HTTP client so the lifecycle, config generation, and
JSON-RPC call shapes are exercised without depending on the binary.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tune_server.audio.formats import SNAPCAST_CAPABILITIES
from tune_server.models import OutputType
from tune_server.outputs.snapcast import SnapcastOutput
from tune_server.zones.snapcast_manager import (
    SNAPSERVER_DEFAULT_AUDIO_PORT,
    SNAPSERVER_DEFAULT_HTTP_PORT,
    SnapcastClient,
    SnapcastManager,
)


# ---------------------------------------------------------------------------
# Static / config tests — no subprocess needed
# ---------------------------------------------------------------------------


def test_output_type_enum_includes_snapcast():
    assert OutputType.SNAPCAST.value == "snapcast"
    assert OutputType.SNAPCAST in OutputType


def test_capabilities_exposes_snapcast_profile():
    assert SNAPCAST_CAPABILITIES.max_sample_rate == 96000
    assert SNAPCAST_CAPABILITIES.max_bit_depth == 24
    assert SNAPCAST_CAPABILITIES.supports_gapless is False


def test_is_supported_matches_platform(tmp_path):
    mgr = SnapcastManager(runtime_dir=tmp_path)
    if sys.platform in ("linux", "darwin"):
        assert mgr.is_supported is True
    else:
        assert mgr.is_supported is False


def test_binary_path_resolves_explicit_override(tmp_path):
    fake = tmp_path / "snapserver"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    mgr = SnapcastManager(runtime_dir=tmp_path, binary=fake)
    assert mgr.binary_path == fake


def test_binary_path_returns_none_when_missing(tmp_path):
    # Override PATH so shutil.which("snapserver") fails.
    with patch.dict(os.environ, {"PATH": str(tmp_path)}, clear=False):
        mgr = SnapcastManager(runtime_dir=tmp_path, binary=None)
        # tmp_path doesn't contain snapserver — shutil.which returns None.
        assert mgr.binary_path is None


def test_write_config_emits_per_zone_streams(tmp_path):
    mgr = SnapcastManager(runtime_dir=tmp_path)
    mgr._streams = {
        7: {"stream_name": "tune-zone-7", "sample_rate": 44100, "bit_depth": 16},
        12: {"stream_name": "tune-zone-12", "sample_rate": 96000, "bit_depth": 24},
    }
    mgr._write_config()
    text = mgr.config_path.read_text()
    assert "[server]" in text
    assert f"port = {SNAPSERVER_DEFAULT_HTTP_PORT}" in text
    assert "tune-zone-7" in text
    assert "44100:16:2" in text
    assert "tune-zone-12" in text
    assert "96000:24:2" in text


def test_write_config_emits_placeholder_when_no_streams(tmp_path):
    mgr = SnapcastManager(runtime_dir=tmp_path)
    mgr._streams = {}
    mgr._write_config()
    text = mgr.config_path.read_text()
    # snapserver requires at least one stream — we emit a placeholder
    # so the daemon can boot before any zone is registered.
    assert "tune-placeholder" in text


# ---------------------------------------------------------------------------
# Stream registration — FIFO creation + restart scheduling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_stream_creates_fifo_and_schedules_restart(tmp_path):
    mgr = SnapcastManager(runtime_dir=tmp_path)
    # _proc=None means the restart task short-circuits; we only check
    # that the side-effects on _streams + filesystem fired.
    stream_name, fifo = await mgr.register_stream(
        zone_id=42, sample_rate=44100, bit_depth=16,
    )
    assert stream_name == "tune-zone-42"
    assert fifo == tmp_path / "snapfifo-42"
    assert fifo.exists()
    assert mgr._streams[42]["sample_rate"] == 44100
    assert mgr._restart_pending is not None
    # Cancel the pending task so pytest doesn't warn about a leaked
    # coroutine.
    mgr._restart_pending.cancel()
    try:
        await mgr._restart_pending
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_unregister_stream_drops_state_and_fifo(tmp_path):
    mgr = SnapcastManager(runtime_dir=tmp_path)
    await mgr.register_stream(zone_id=99, sample_rate=44100, bit_depth=16)
    if mgr._restart_pending is not None:
        mgr._restart_pending.cancel()
        try:
            await mgr._restart_pending
        except asyncio.CancelledError:
            pass

    await mgr.unregister_stream(zone_id=99)
    assert 99 not in mgr._streams
    assert not (tmp_path / "snapfifo-99").exists()
    if mgr._restart_pending is not None:
        mgr._restart_pending.cancel()
        try:
            await mgr._restart_pending
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# JSON-RPC — list_clients parses Server.GetStatus shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_clients_parses_get_status(tmp_path):
    mgr = SnapcastManager(runtime_dir=tmp_path)
    fake_status = {
        "server": {
            "groups": [
                {
                    "id": "group-A",
                    "stream_id": "tune-zone-1",
                    "clients": [
                        {
                            "id": "client-uuid-A",
                            "connected": True,
                            "host": {"name": "kitchen-pi", "mac": "aa:bb:cc:dd:ee:ff"},
                            "config": {
                                "name": "Kitchen",
                                "volume": {"muted": False, "percent": 75},
                            },
                        },
                        {
                            "id": "client-uuid-B",
                            "connected": False,
                            "host": {"name": "bedroom-pi", "mac": "11:22:33:44:55:66"},
                            "config": {
                                "name": "Bedroom",
                                "volume": {"muted": True, "percent": 0},
                            },
                        },
                    ],
                },
            ],
        },
    }

    async def fake_rpc(method, params=None):
        assert method == "Server.GetStatus"
        return fake_status

    # Fake out _rpc + the session presence check.
    mgr._rpc_session = MagicMock()
    mgr._rpc = fake_rpc  # type: ignore[assignment]

    clients = await mgr.list_clients()
    by_id = {c.id: c for c in clients}
    assert by_id["client-uuid-A"].name == "Kitchen"
    assert by_id["client-uuid-A"].connected is True
    assert by_id["client-uuid-A"].volume == 75
    assert by_id["client-uuid-B"].connected is False
    assert by_id["client-uuid-B"].volume == 0


@pytest.mark.asyncio
async def test_set_client_volume_clamps_and_calls_rpc(tmp_path):
    mgr = SnapcastManager(runtime_dir=tmp_path)
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(method, params=None):
        calls.append((method, dict(params or {})))
        return None

    mgr._rpc_session = MagicMock()
    mgr._rpc = fake_rpc  # type: ignore[assignment]

    await mgr.set_client_volume("client-A", 250)  # over 100 -> 100
    await mgr.set_client_volume("client-A", -10)  # under 0 -> 0
    assert calls[0][0] == "Client.SetVolume"
    assert calls[0][1]["volume"]["percent"] == 100
    assert calls[1][1]["volume"]["percent"] == 0
    assert calls[1][1]["volume"]["muted"] is True


# ---------------------------------------------------------------------------
# SnapcastOutput — write/stop semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_write_drops_when_no_reader(tmp_path):
    """Without a snapserver reader on the FIFO, write must not block —
    it should silently drop the buffer (snapclient sees ~1 s of silence
    until subscribed)."""
    fifo = tmp_path / "snapfifo-test"
    os.mkfifo(fifo, 0o666)
    mgr = MagicMock()
    out = SnapcastOutput(
        manager=mgr, stream_name="tune-zone-test", fifo_path=fifo,
        client_ids=[],
    )
    # No FIFO reader — open(O_WRONLY|O_NONBLOCK) raises ENXIO; write
    # path drops silently.
    await out.write(b"\x00\x00" * 1024)
    # No exception, no fd opened.
    assert out._fifo_fd is None


@pytest.mark.asyncio
async def test_output_set_volume_translates_to_per_client_calls(tmp_path):
    fifo = tmp_path / "snapfifo-vol"
    os.mkfifo(fifo, 0o666)
    mgr = MagicMock()
    mgr.set_client_volume = AsyncMock()
    out = SnapcastOutput(
        manager=mgr, stream_name="tune-zone-vol", fifo_path=fifo,
        client_ids=["uuid-A", "uuid-B"],
    )
    await out.set_volume(0.5)
    # 0.5 -> 50 (snapcast 0-100 scale), one call per client UUID.
    assert mgr.set_client_volume.await_count == 2
    mgr.set_client_volume.assert_any_await("uuid-A", 50)
    mgr.set_client_volume.assert_any_await("uuid-B", 50)


@pytest.mark.asyncio
async def test_output_close_releases_fifo(tmp_path):
    fifo = tmp_path / "snapfifo-close"
    os.mkfifo(fifo, 0o666)
    mgr = MagicMock()
    out = SnapcastOutput(
        manager=mgr, stream_name="tune-zone-close", fifo_path=fifo,
        client_ids=[],
    )
    # Fake an open fd so close has something to clean up.
    out._fifo_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    await out.close()
    assert out._fifo_fd is None


# ---------------------------------------------------------------------------
# REST: /snapcast/clients/{id}/assign — POST + DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_endpoint_persists_and_calls_rpc():
    """POST /snapcast/clients/{id}/assign should append the UUID to
    Zone.snapcast_client_ids and route the snapcast group via JSON-RPC."""
    from tune_server.api.deps import deps
    from tune_server.api.routes.snapcast import (
        AssignClientRequest, assign_client,
    )

    mgr = MagicMock()
    mgr.is_supported = True
    mgr.set_clients_for_stream = AsyncMock()

    zone_repo = MagicMock()
    zone_repo.get = AsyncMock(return_value={
        "id": 7, "name": "Living Room", "output_type": "snapcast",
        "snapcast_client_ids": None,  # legacy: no clients yet
        "snapcast_stream_name": None,
    })
    zone_repo.update = AsyncMock()

    original_mgr = deps.snapcast_manager
    original_repo = deps.zone_repo
    deps.snapcast_manager = mgr
    deps.zone_repo = zone_repo
    try:
        result = await assign_client(
            client_id="uuid-A", body=AssignClientRequest(zone_id=7),
        )
    finally:
        deps.snapcast_manager = original_mgr
        deps.zone_repo = original_repo

    assert result["client_ids"] == ["uuid-A"]
    assert result["stream_name"] == "tune-zone-7"
    zone_repo.update.assert_awaited_once()
    update_kwargs = zone_repo.update.await_args.kwargs
    import json as _json
    assert _json.loads(update_kwargs["snapcast_client_ids"]) == ["uuid-A"]
    mgr.set_clients_for_stream.assert_awaited_once_with("tune-zone-7", ["uuid-A"])


@pytest.mark.asyncio
async def test_assign_endpoint_appends_to_existing_clients():
    """Assigning a second client should append, not replace."""
    from tune_server.api.deps import deps
    from tune_server.api.routes.snapcast import (
        AssignClientRequest, assign_client,
    )

    mgr = MagicMock()
    mgr.is_supported = True
    mgr.set_clients_for_stream = AsyncMock()

    zone_repo = MagicMock()
    zone_repo.get = AsyncMock(return_value={
        "id": 7, "output_type": "snapcast",
        "snapcast_client_ids": '["uuid-A"]',
        "snapcast_stream_name": "tune-zone-7",
    })
    zone_repo.update = AsyncMock()

    original_mgr, original_repo = deps.snapcast_manager, deps.zone_repo
    deps.snapcast_manager, deps.zone_repo = mgr, zone_repo
    try:
        result = await assign_client(
            client_id="uuid-B", body=AssignClientRequest(zone_id=7),
        )
    finally:
        deps.snapcast_manager, deps.zone_repo = original_mgr, original_repo

    assert result["client_ids"] == ["uuid-A", "uuid-B"]


@pytest.mark.asyncio
async def test_assign_endpoint_rejects_non_snapcast_zone():
    from fastapi import HTTPException
    from tune_server.api.deps import deps
    from tune_server.api.routes.snapcast import (
        AssignClientRequest, assign_client,
    )

    mgr = MagicMock()
    mgr.is_supported = True

    zone_repo = MagicMock()
    zone_repo.get = AsyncMock(return_value={
        "id": 7, "output_type": "dlna",  # wrong type
    })

    original_mgr, original_repo = deps.snapcast_manager, deps.zone_repo
    deps.snapcast_manager, deps.zone_repo = mgr, zone_repo
    try:
        with pytest.raises(HTTPException) as ei:
            await assign_client(
                client_id="uuid-X", body=AssignClientRequest(zone_id=7),
            )
        assert ei.value.status_code == 400
        assert "not_snapcast" in ei.value.detail
    finally:
        deps.snapcast_manager, deps.zone_repo = original_mgr, original_repo


@pytest.mark.asyncio
async def test_unassign_endpoint_removes_client():
    from tune_server.api.deps import deps
    from tune_server.api.routes.snapcast import (
        AssignClientRequest, unassign_client,
    )

    mgr = MagicMock()
    mgr.is_supported = True
    mgr.set_clients_for_stream = AsyncMock()

    zone_repo = MagicMock()
    zone_repo.get = AsyncMock(return_value={
        "id": 7, "output_type": "snapcast",
        "snapcast_client_ids": '["uuid-A", "uuid-B"]',
        "snapcast_stream_name": "tune-zone-7",
    })
    zone_repo.update = AsyncMock()

    original_mgr, original_repo = deps.snapcast_manager, deps.zone_repo
    deps.snapcast_manager, deps.zone_repo = mgr, zone_repo
    try:
        result = await unassign_client(
            client_id="uuid-A", body=AssignClientRequest(zone_id=7),
        )
    finally:
        deps.snapcast_manager, deps.zone_repo = original_mgr, original_repo

    assert result["client_ids"] == ["uuid-B"]
    mgr.set_clients_for_stream.assert_awaited_once_with("tune-zone-7", ["uuid-B"])

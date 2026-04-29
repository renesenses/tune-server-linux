"""GroupManager composite-group wiring (v0.8.0).

Verifies the new CompositeGroup activation/dissolution path that runs
inside GroupManager.create_group / dissolve_group when a ZoneGroup
contains Snapcast or Sonos members.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tune_server.event_bus import EventBus
from tune_server.models import OutputType, PlaybackState
from tune_server.zones.group import GroupManager


def _fake_zone_instance(
    zone_id: int,
    output_type: OutputType,
    output_device_id: str | None = None,
    *,
    snapcast_stream_name: str | None = None,
    snapcast_client_ids: list[str] | None = None,
) -> MagicMock:
    zi = MagicMock()
    zi.zone_id = zone_id
    zi.output_type = output_type
    zi.output_device_id = output_device_id
    zi.snapcast_stream_name = snapcast_stream_name
    zi.snapcast_client_ids = snapcast_client_ids or []
    zi.player.state = PlaybackState.STOPPED
    zi.player.current_track = None
    zi.position_ms = 0
    zi.group_id = None
    return zi


@pytest.mark.asyncio
async def test_create_group_skips_composite_when_no_typed_managers():
    bus = EventBus()
    mgr = GroupManager(bus)
    # Don't wire any manager — composite should be a complete no-op,
    # legacy path still functions.
    leader = _fake_zone_instance(1, OutputType.LOCAL)
    follower = _fake_zone_instance(2, OutputType.DLNA, output_device_id="dlna-uuid")
    group = await mgr.create_group(leader, [follower])
    assert group.group_id in mgr._groups
    # No composite tracked — None of the techs need native sync.
    assert group.group_id not in mgr._composite_groups


@pytest.mark.asyncio
async def test_create_group_skips_composite_for_pure_local_dlna():
    bus = EventBus()
    mgr = GroupManager(bus)
    snap = MagicMock()
    snap.set_clients_for_stream = AsyncMock()
    sonos = MagicMock()
    sonos.set_group = AsyncMock()
    mgr.set_runtime_managers(snapcast_manager=snap, sonos_manager=sonos)

    # Group of LOCAL + DLNA — no Snapcast/Sonos zones, composite should
    # not activate (saves the JSON-RPC roundtrip).
    leader = _fake_zone_instance(1, OutputType.LOCAL)
    follower = _fake_zone_instance(2, OutputType.DLNA)
    group = await mgr.create_group(leader, [follower])

    snap.set_clients_for_stream.assert_not_called()
    sonos.set_group.assert_not_called()
    assert group.group_id not in mgr._composite_groups


@pytest.mark.asyncio
async def test_create_group_activates_snapcast_native_sync():
    bus = EventBus()
    mgr = GroupManager(bus)
    snap = MagicMock()
    snap.set_clients_for_stream = AsyncMock()
    mgr.set_runtime_managers(snapcast_manager=snap)

    leader = _fake_zone_instance(
        1, OutputType.SNAPCAST,
        snapcast_stream_name="tune-zone-1",
        snapcast_client_ids=["uuid-A"],
    )
    follower = _fake_zone_instance(
        2, OutputType.SNAPCAST,
        snapcast_stream_name="tune-zone-2",
        snapcast_client_ids=["uuid-B"],
    )
    group = await mgr.create_group(leader, [follower])
    # Composite tracked + snapcast group merged onto leader stream.
    assert group.group_id in mgr._composite_groups
    snap.set_clients_for_stream.assert_awaited()
    args = snap.set_clients_for_stream.await_args.args
    assert args[0] == "tune-zone-1"  # leader's stream
    assert sorted(args[1]) == ["uuid-A", "uuid-B"]


@pytest.mark.asyncio
async def test_create_group_activates_sonos_native_sync():
    bus = EventBus()
    mgr = GroupManager(bus)
    sonos = MagicMock()
    sonos.set_group = AsyncMock()
    mgr.set_runtime_managers(sonos_manager=sonos)

    leader = _fake_zone_instance(
        1, OutputType.SONOS, output_device_id="RINCON_LEADER",
    )
    follower = _fake_zone_instance(
        2, OutputType.SONOS, output_device_id="RINCON_FOLLOWER",
    )
    await mgr.create_group(leader, [follower])
    sonos.set_group.assert_awaited_once_with(
        "RINCON_LEADER", ["RINCON_LEADER", "RINCON_FOLLOWER"],
    )


@pytest.mark.asyncio
async def test_play_with_composite_offsets_orders_techs_by_delay(monkeypatch):
    """Mixed-tech ZoneGroup.play() should consult CompositeGroup for
    per-tech start offsets and start each tech in ascending offset
    order, sleeping the delta between consecutive techs."""
    bus = EventBus()
    mgr = GroupManager(bus)
    snap = MagicMock()
    snap.set_clients_for_stream = AsyncMock()
    sonos = MagicMock()
    sonos.set_group = AsyncMock()
    fake_db = MagicMock()
    mgr.set_runtime_managers(
        snapcast_manager=snap, sonos_manager=sonos, db=fake_db,
    )

    snap_zone = _fake_zone_instance(
        1, OutputType.SNAPCAST,
        snapcast_stream_name="tune-zone-1",
        snapcast_client_ids=["uuid-A"],
    )
    snap_zone.player.play = AsyncMock()
    sonos_zone = _fake_zone_instance(
        2, OutputType.SONOS, output_device_id="RINCON_X",
    )
    sonos_zone.player.play = AsyncMock()

    group = await mgr.create_group(snap_zone, [sonos_zone])

    # Patch GroupDelayRepo.get_delay_ms so we don't need a real DB:
    # canonical pair (snapcast, sonos) → 150 ms means Sonos lags
    # Snapcast by 150 ms.
    from tune_server.zones import composite_group as cg_mod

    async def fake_get_delay_ms(self, a, b):
        return 150

    monkeypatch.setattr(cg_mod.GroupDelayRepo, "get_delay_ms", fake_get_delay_ms)

    # Stub asyncio.sleep so the test doesn't actually wait 150 ms.
    sleeps: list[float] = []

    import asyncio as _asyncio
    real_sleep = _asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)

    await group.play(tracks=[], start_position=0)

    # Snapcast has the smaller offset (0 ms), Sonos has 150 ms. The
    # delta sleep between them should be ~0.15 s.
    assert any(abs(s - 0.15) < 1e-9 for s in sleeps)
    snap_zone.player.play.assert_awaited_once()
    sonos_zone.player.play.assert_awaited_once()


@pytest.mark.asyncio
async def test_play_with_homogeneous_composite_skips_offset_path():
    """Homogeneous Sonos-only group must not call start_playback() —
    native S2 sync handles intra-group alignment."""
    bus = EventBus()
    mgr = GroupManager(bus)
    sonos = MagicMock()
    sonos.set_group = AsyncMock()
    fake_db = MagicMock()
    mgr.set_runtime_managers(sonos_manager=sonos, db=fake_db)

    leader = _fake_zone_instance(1, OutputType.SONOS, output_device_id="RINCON_A")
    leader.player.play = AsyncMock()
    follower = _fake_zone_instance(2, OutputType.SONOS, output_device_id="RINCON_B")
    follower.player.play = AsyncMock()

    group = await mgr.create_group(leader, [follower])
    # Patch start_playback to be sure it's not called.
    from tune_server.zones.composite_group import CompositeGroup
    group._composite.start_playback = AsyncMock(  # type: ignore[attr-defined]
        side_effect=AssertionError("start_playback should not be called for homogeneous"),
    )

    await group.play(tracks=[], start_position=0)
    # Both players started — no offset path taken.
    leader.player.play.assert_awaited_once()
    follower.player.play.assert_awaited_once()


@pytest.mark.asyncio
async def test_dissolve_group_tears_down_composite():
    bus = EventBus()
    mgr = GroupManager(bus)
    sonos = MagicMock()
    sonos.set_group = AsyncMock()
    sonos.unjoin = AsyncMock()
    mgr.set_runtime_managers(sonos_manager=sonos)

    leader = _fake_zone_instance(1, OutputType.SONOS, output_device_id="RINCON_A")
    follower = _fake_zone_instance(2, OutputType.SONOS, output_device_id="RINCON_B")
    group = await mgr.create_group(leader, [follower])

    await mgr.dissolve_group(group.group_id)
    # Both speakers unjoined from native group.
    assert sonos.unjoin.await_count == 2
    assert group.group_id not in mgr._composite_groups

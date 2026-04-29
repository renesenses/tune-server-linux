"""Tests for CompositeGroup + GroupDelayRepo (v0.8.0 multi-room).

Mock the typed groups + DB so we exercise composition logic without
needing real SoCo / Snapcast / database.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tune_server.models import OutputType
from tune_server.zones.composite_group import (
    CompositeGroup, GroupDelayRepo, _canonical_pair,
)
from tune_server.zones.typed_groups import (
    LocalGroup, SnapcastGroup, SonosGroup, build_typed_groups,
)


# ---------------------------------------------------------------------------
# Canonical pair ordering — bedrock for the rest
# ---------------------------------------------------------------------------


def test_canonical_pair_sorts_alphabetically():
    assert _canonical_pair(OutputType.SONOS, OutputType.SNAPCAST) == ("snapcast", "sonos")
    assert _canonical_pair("sonos", "snapcast") == ("snapcast", "sonos")
    # Same input twice: still sorted, gives degenerate pair.
    assert _canonical_pair("local", "local") == ("local", "local")


# ---------------------------------------------------------------------------
# build_typed_groups — bucket zones by techno
# ---------------------------------------------------------------------------


def test_build_typed_groups_buckets_by_technology():
    zones = [
        SimpleNamespace(id=1, output_type=OutputType.SONOS, output_device_id="RINCON_A"),
        SimpleNamespace(id=2, output_type=OutputType.SONOS, output_device_id="RINCON_B"),
        SimpleNamespace(id=3, output_type=OutputType.SNAPCAST,
                        snapcast_stream_name="tune-zone-3", snapcast_client_ids=["uuid-X"]),
        SimpleNamespace(id=4, output_type=OutputType.LOCAL),
    ]
    groups = build_typed_groups(zones)
    by_tech = {g.technology: g for g in groups}
    assert isinstance(by_tech[OutputType.SONOS], SonosGroup)
    assert isinstance(by_tech[OutputType.SNAPCAST], SnapcastGroup)
    assert isinstance(by_tech[OutputType.LOCAL], LocalGroup)
    assert sorted(by_tech[OutputType.SONOS].members) == [1, 2]
    assert by_tech[OutputType.SNAPCAST].members == [3]
    assert by_tech[OutputType.LOCAL].members == [4]


# ---------------------------------------------------------------------------
# CompositeGroup — homogeneous + mixed playback offsets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_homogeneous_returns_zero_offset():
    cg = CompositeGroup(
        name="kitchen",
        typed_groups=[SonosGroup(name="sonos", members=[1, 2])],
    )
    delay_repo = MagicMock()
    delay_repo.get_delay_ms = AsyncMock(return_value=999)  # should be ignored
    offsets = await cg.start_playback(
        manager_bag={"sonos": _fake_sonos_mgr()},
        zones_by_id=_fake_zones_sonos(),
        delay_repo=delay_repo,
    )
    assert offsets == {OutputType.SONOS: 0}
    delay_repo.get_delay_ms.assert_not_called()


@pytest.mark.asyncio
async def test_composite_mixed_applies_pair_delay_once():
    """Sonos + Snapcast in the same composite group → one DB lookup
    (not per-track), delay applied on whichever tech is alphabetically
    second when delay_ms > 0."""
    sonos_grp = SonosGroup(name="s", members=[1])
    snap_grp = SnapcastGroup(name="sc", members=[2])
    cg = CompositeGroup(name="party", typed_groups=[sonos_grp, snap_grp])

    # Canonical: snapcast < sonos. delay_ms > 0 means sonos lags.
    delay_repo = MagicMock()
    delay_repo.get_delay_ms = AsyncMock(return_value=120)

    offsets = await cg.start_playback(
        manager_bag={
            "sonos": _fake_sonos_mgr(),
            "snapcast": _fake_snap_mgr(),
        },
        zones_by_id={**_fake_zones_sonos(), **_fake_zones_snap()},
        delay_repo=delay_repo,
    )
    # Sonos is alphabetically second + delay_ms positive → sonos delays.
    assert offsets[OutputType.SONOS] == 120
    assert offsets[OutputType.SNAPCAST] == 0
    delay_repo.get_delay_ms.assert_awaited_once()


@pytest.mark.asyncio
async def test_composite_dissolve_calls_each_typed_group():
    sonos_grp = SonosGroup(name="s", members=[1, 2])
    snap_grp = SnapcastGroup(name="sc", members=[3])
    cg = CompositeGroup(name="party", typed_groups=[sonos_grp, snap_grp])

    sonos = _fake_sonos_mgr()
    snap = _fake_snap_mgr()
    await cg.dissolve(
        manager_bag={"sonos": sonos, "snapcast": snap},
        zones_by_id={**_fake_zones_sonos(), **_fake_zones_snap()},
    )
    sonos.unjoin.assert_awaited()  # at least once per Sonos member
    # Snapcast dissolve restores per-zone streams via set_clients_for_stream.
    snap.set_clients_for_stream.assert_awaited()


# ---------------------------------------------------------------------------
# GroupDelayRepo — canonicalisation + upsert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_delay_repo_canonicalises_pair_on_get():
    db = MagicMock()
    db.fetchone = AsyncMock(return_value={"delay_ms": 80})
    repo = GroupDelayRepo(db)

    # Both orders should hit the same SQL (canonicalised).
    await repo.get_delay_ms("sonos", "snapcast")
    await repo.get_delay_ms("snapcast", "sonos")
    # Both calls passed alphabetically-sorted (snapcast, sonos).
    for call in db.fetchone.await_args_list:
        args = call.args[1]  # tuple of params
        assert args == ("snapcast", "sonos")


@pytest.mark.asyncio
async def test_group_delay_repo_returns_zero_for_same_techno():
    db = MagicMock()
    repo = GroupDelayRepo(db)
    # Same techno → no DB call.
    assert await repo.get_delay_ms("sonos", "sonos") == 0
    db.fetchone.assert_not_called()


@pytest.mark.asyncio
async def test_group_delay_repo_upsert_inserts_then_updates():
    db = MagicMock()
    db.fetchone = AsyncMock(return_value=None)  # no existing row
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    repo = GroupDelayRepo(db)
    await repo.set_delay_ms("sonos", "snapcast", 75)
    sql_call = db.execute.await_args.args[0]
    assert "INSERT INTO group_delays" in sql_call
    db.commit.assert_awaited()

    # Now simulate an existing row → UPDATE branch.
    db.fetchone = AsyncMock(return_value={"id": 1})
    db.execute.reset_mock()
    db.commit.reset_mock()
    await repo.set_delay_ms("snapcast", "sonos", 90)  # reverse order
    sql_call = db.execute.await_args.args[0]
    assert "UPDATE group_delays" in sql_call
    db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fake_sonos_mgr() -> MagicMock:
    m = MagicMock()
    m.set_group = AsyncMock()
    m.unjoin = AsyncMock()
    return m


def _fake_snap_mgr() -> MagicMock:
    m = MagicMock()
    m.set_clients_for_stream = AsyncMock()
    return m


def _fake_zones_sonos() -> dict:
    return {
        1: SimpleNamespace(id=1, output_type=OutputType.SONOS, output_device_id="RINCON_A"),
        2: SimpleNamespace(id=2, output_type=OutputType.SONOS, output_device_id="RINCON_B"),
    }


def _fake_zones_snap() -> dict:
    return {
        3: SimpleNamespace(id=3, output_type=OutputType.SNAPCAST,
                           snapcast_stream_name="tune-zone-3",
                           snapcast_client_ids=["uuid-X", "uuid-Y"]),
    }

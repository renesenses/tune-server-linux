from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from tune_server.models import PlaybackState
from tune_server.zones.group import GroupManager, ZoneGroup
from tune_server.zones.sync import CORRECTION_COOLDOWN_S, DRIFT_THRESHOLD_MS, SyncEngine


def _make_mock_zone(zone_id, state=PlaybackState.PLAYING, position_ms=10000):
    zone = MagicMock()
    zone.zone_id = zone_id
    zone.player = AsyncMock()
    zone.player.state = state
    type(zone).position_ms = PropertyMock(return_value=position_ms)
    return zone


def _make_group(leader, followers, group_id="grp-1"):
    from tune_server.event_bus import EventBus
    group = ZoneGroup(group_id, leader, followers, EventBus())
    group._last_play_time = 0  # Not recently played
    return group


@pytest.fixture
def gm(event_bus):
    return GroupManager(event_bus)


async def test_sync_no_groups(gm):
    engine = SyncEngine(gm)
    # Should not crash with no groups
    for group in gm.list_groups():
        await engine._sync_group(group)


async def test_sync_leader_not_playing(gm, event_bus):
    leader = _make_mock_zone(1, state=PlaybackState.STOPPED, position_ms=5000)
    follower = _make_mock_zone(2, state=PlaybackState.PLAYING, position_ms=0)
    group = _make_group(leader, [follower])

    engine = SyncEngine(gm)
    await engine._sync_group(group)
    # Follower should not be sought
    follower.player.seek.assert_not_awaited()


async def test_sync_leader_position_zero(gm, event_bus):
    leader = _make_mock_zone(1, state=PlaybackState.PLAYING, position_ms=0)
    follower = _make_mock_zone(2, state=PlaybackState.PLAYING, position_ms=5000)
    group = _make_group(leader, [follower])

    engine = SyncEngine(gm)
    await engine._sync_group(group)
    follower.player.seek.assert_not_awaited()


async def test_sync_within_threshold(gm, event_bus):
    leader = _make_mock_zone(1, position_ms=10000)
    follower = _make_mock_zone(2, position_ms=10500)  # 500ms drift < 1000ms threshold
    group = _make_group(leader, [follower])

    engine = SyncEngine(gm)
    await engine._sync_group(group)
    follower.player.seek.assert_not_awaited()


async def test_sync_exceeds_threshold(gm, event_bus):
    leader = _make_mock_zone(1, position_ms=10000)
    follower = _make_mock_zone(2, position_ms=12000)  # 2000ms drift > 1000ms threshold
    group = _make_group(leader, [follower])

    engine = SyncEngine(gm)
    await engine._sync_group(group)
    follower.player.seek.assert_awaited_once_with(10000)


async def test_sync_cooldown(gm, event_bus):
    leader = _make_mock_zone(1, position_ms=10000)
    follower = _make_mock_zone(2, position_ms=12000)
    group = _make_group(leader, [follower])

    engine = SyncEngine(gm)
    # First correction goes through
    await engine._sync_group(group)
    follower.player.seek.assert_awaited_once()

    # Second correction within cooldown should be skipped
    follower.player.seek.reset_mock()
    await engine._sync_group(group)
    follower.player.seek.assert_not_awaited()


async def test_sync_after_play_cooldown(gm, event_bus):
    leader = _make_mock_zone(1, position_ms=10000)
    follower = _make_mock_zone(2, position_ms=12000)
    group = _make_group(leader, [follower])
    group._last_play_time = time.monotonic()  # Just played

    engine = SyncEngine(gm)
    await engine._sync_group(group)
    follower.player.seek.assert_not_awaited()


async def test_start_stop(gm):
    engine = SyncEngine(gm)
    await engine.start()
    assert engine._task is not None
    assert engine._running is True

    await engine.stop()
    assert engine._task is None
    assert engine._running is False

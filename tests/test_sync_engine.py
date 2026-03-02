from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from tune_server.models import PlaybackState
from tune_server.zones.group import GroupManager, ZoneGroup
from tune_server.zones.sync import SyncEngine


def _make_mock_zone(zone_id, state=PlaybackState.PLAYING, position_ms=10000):
    zone = MagicMock()
    zone.zone_id = zone_id
    zone.player = AsyncMock()
    zone.player.state = state
    zone.sync_delay_ms = 0
    zone.output = AsyncMock()
    zone.output.get_position_ms = AsyncMock(return_value=position_ms)
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
    follower = _make_mock_zone(2, position_ms=10400)  # 400ms drift < 500ms threshold
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


async def test_sync_with_positive_offset(gm, event_bus):
    leader = _make_mock_zone(1, position_ms=10000)
    follower = _make_mock_zone(2, position_ms=12000)
    follower.sync_delay_ms = 500
    group = _make_group(leader, [follower])

    engine = SyncEngine(gm)
    await engine._sync_group(group)
    follower.player.seek.assert_awaited_once_with(10500)


async def test_sync_with_negative_offset(gm, event_bus):
    leader = _make_mock_zone(1, position_ms=10000)
    follower = _make_mock_zone(2, position_ms=12000)
    follower.sync_delay_ms = -500
    group = _make_group(leader, [follower])

    engine = SyncEngine(gm)
    await engine._sync_group(group)
    follower.player.seek.assert_awaited_once_with(9500)


async def test_get_zone_position_ms_from_output(gm):
    engine = SyncEngine(gm)
    zone = _make_mock_zone(1, position_ms=3000)
    zone.output.get_position_ms = AsyncMock(return_value=5000)
    result = await engine._get_zone_position_ms(zone)
    assert result == 5000


async def test_get_zone_position_ms_fallback(gm):
    engine = SyncEngine(gm)
    zone = _make_mock_zone(1, position_ms=3000)
    zone.output.get_position_ms = AsyncMock(return_value=-1)
    result = await engine._get_zone_position_ms(zone)
    assert result == 3000


async def test_has_active_groups(gm, event_bus):
    leader_playing = _make_mock_zone(1, state=PlaybackState.PLAYING)
    follower = _make_mock_zone(2)
    group = _make_group(leader_playing, [follower])
    gm._groups[group.group_id] = group

    engine = SyncEngine(gm)
    assert engine._has_active_groups() is True

    gm._groups.clear()
    leader_stopped = _make_mock_zone(3, state=PlaybackState.STOPPED)
    group2 = _make_group(leader_stopped, [follower])
    gm._groups[group2.group_id] = group2
    assert engine._has_active_groups() is False


async def test_start_stop(gm):
    engine = SyncEngine(gm)
    await engine.start()
    assert engine._task is not None
    assert engine._running is True

    await engine.stop()
    assert engine._task is None
    assert engine._running is False

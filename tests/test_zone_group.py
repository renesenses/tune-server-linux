from __future__ import annotations

from unittest.mock import AsyncMock, PropertyMock, MagicMock

import pytest

from tune_server.audio.formats import LOCAL_CAPABILITIES
from tune_server.event_bus import EventBus, EventType
from tune_server.models import OutputType, PlaybackState
from tune_server.playback.queue import PlayQueue
from tune_server.zones.group import GroupManager, ZoneGroup


def _make_mock_zone(zone_id, name="Zone"):
    """Create a mock ZoneInstance."""
    zone = MagicMock()
    zone.zone_id = zone_id
    zone.name = name
    zone.output_type = OutputType.LOCAL
    zone.group_id = None
    zone.player = AsyncMock()
    zone.player.state = PlaybackState.STOPPED
    zone.player.current_track = None
    zone.player.queue = MagicMock(spec=PlayQueue)
    zone.player.queue.length = 0
    return zone


@pytest.fixture
def leader():
    return _make_mock_zone(1, "Leader")


@pytest.fixture
def follower1():
    return _make_mock_zone(2, "Follower 1")


@pytest.fixture
def follower2():
    return _make_mock_zone(3, "Follower 2")


@pytest.fixture
def group(event_bus, leader, follower1):
    return ZoneGroup("grp-1", leader, [follower1], event_bus)


# --- ZoneGroup ---


def test_group_properties(group, leader, follower1):
    assert group.group_id == "grp-1"
    assert group.leader is leader
    assert follower1 in group.followers


def test_all_zones_includes_leader_and_followers(group, leader, follower1):
    all_zones = group.all_zones
    assert leader in all_zones
    assert follower1 in all_zones
    assert len(all_zones) == 2


def test_zone_ids(group):
    ids = group.zone_ids
    assert 1 in ids
    assert 2 in ids


async def test_pause_calls_all_zones(group, leader, follower1):
    await group.pause()
    leader.player.pause.assert_awaited_once()
    follower1.player.pause.assert_awaited_once()


async def test_resume_calls_all_zones(group, leader, follower1):
    await group.resume()
    leader.player.resume.assert_awaited_once()
    follower1.player.resume.assert_awaited_once()


async def test_stop_calls_all_zones(group, leader, follower1):
    await group.stop()
    leader.player.stop.assert_awaited_once()
    follower1.player.stop.assert_awaited_once()


async def test_skip_next_all_zones(group, leader, follower1):
    await group.skip_next()
    leader.player.skip_next.assert_awaited_once()
    follower1.player.skip_next.assert_awaited_once()


def test_add_follower(group, follower2):
    group.add_follower(follower2)
    assert follower2 in group.followers
    assert follower2.group_id == "grp-1"


def test_add_follower_rejects_leader(group, leader):
    original_count = len(group.followers)
    group.add_follower(leader)
    assert len(group.followers) == original_count


def test_add_follower_rejects_duplicate(group, follower1):
    original_count = len(group.followers)
    group.add_follower(follower1)
    assert len(group.followers) == original_count


def test_remove_follower(group, follower1):
    group.remove_follower(follower1)
    assert follower1 not in group.followers
    assert follower1.group_id is None


# --- GroupManager ---


async def test_create_group(group_manager, leader, follower1):
    group = await group_manager.create_group(leader, [follower1])
    assert group.group_id is not None
    assert leader.group_id == group.group_id
    assert follower1.group_id == group.group_id


async def test_dissolve_group(group_manager, leader, follower1):
    group = await group_manager.create_group(leader, [follower1])
    gid = group.group_id

    await group_manager.dissolve_group(gid)

    assert leader.group_id is None
    assert follower1.group_id is None
    assert group_manager.get_group(gid) is None


async def test_get_group_for_zone(group_manager, leader, follower1):
    group = await group_manager.create_group(leader, [follower1])
    found = group_manager.get_group_for_zone(1)
    assert found is not None
    assert found.group_id == group.group_id


async def test_get_group_for_zone_not_found(group_manager):
    assert group_manager.get_group_for_zone(999) is None


async def test_list_groups(group_manager, leader, follower1):
    await group_manager.create_group(leader, [follower1])
    groups = group_manager.list_groups()
    assert len(groups) == 1

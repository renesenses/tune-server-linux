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


# ---------------------------------------------------------------------------
# Group sync: play/pause/stop/skip propagation
# ---------------------------------------------------------------------------


async def test_play_propagates_to_all_zones(group, leader, follower1):
    """play() should call play on leader and all followers."""
    from tune_server.models import Track

    tracks = [Track(title="Song", track_number=1)]
    await group.play(tracks)
    leader.player.play.assert_awaited_once()
    follower1.player.play.assert_awaited_once()


async def test_skip_previous_propagates(group, leader, follower1):
    """skip_previous() should propagate to all zones."""
    await group.skip_previous()
    leader.player.skip_previous.assert_awaited_once()
    follower1.player.skip_previous.assert_awaited_once()


async def test_pause_continues_on_error(event_bus, leader, follower1, follower2):
    """If one zone raises during pause, the others should still be paused."""
    leader.player.pause = AsyncMock(side_effect=RuntimeError("oops"))

    group = ZoneGroup("grp-err", leader, [follower1, follower2], event_bus)
    await group.pause()

    # leader raised, but followers should still have been paused
    follower1.player.pause.assert_awaited_once()
    follower2.player.pause.assert_awaited_once()


async def test_resume_continues_on_error(event_bus, leader, follower1, follower2):
    """If one zone raises during resume, the others should still be resumed."""
    follower1.player.resume = AsyncMock(side_effect=RuntimeError("boom"))

    group = ZoneGroup("grp-err2", leader, [follower1, follower2], event_bus)
    await group.resume()

    leader.player.resume.assert_awaited_once()
    follower2.player.resume.assert_awaited_once()


async def test_stop_continues_on_error(event_bus, leader, follower1, follower2):
    """If one zone raises during stop, the others should still be stopped."""
    follower2.player.stop = AsyncMock(side_effect=RuntimeError("fail"))

    group = ZoneGroup("grp-err3", leader, [follower1, follower2], event_bus)
    await group.stop()

    leader.player.stop.assert_awaited_once()
    follower1.player.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# Group follower management edge cases
# ---------------------------------------------------------------------------


def test_add_multiple_followers(event_bus, leader, follower1, follower2):
    """Adding multiple followers sequentially should work."""
    group = ZoneGroup("grp-multi", leader, [], event_bus)
    group.add_follower(follower1)
    group.add_follower(follower2)
    assert len(group.followers) == 2
    assert follower1.group_id == "grp-multi"
    assert follower2.group_id == "grp-multi"


def test_remove_follower_not_in_group(group, follower2):
    """Removing a follower that's not in the group should be a no-op."""
    original_count = len(group.followers)
    group.remove_follower(follower2)
    assert len(group.followers) == original_count


# ---------------------------------------------------------------------------
# GroupManager: dissolve cleans up all zones
# ---------------------------------------------------------------------------


async def test_dissolve_group_emits_event(group_manager, event_bus, leader, follower1):
    """Dissolving a group should emit ZONE_UNGROUPED event."""
    events = []
    event_bus.on(EventType.ZONE_UNGROUPED, lambda e: events.append(e))

    group = await group_manager.create_group(leader, [follower1])
    gid = group.group_id

    await group_manager.dissolve_group(gid)
    assert len(events) == 1
    assert events[0].data["group_id"] == gid


async def test_dissolve_nonexistent_group_noop(group_manager):
    """Dissolving a non-existent group should silently do nothing."""
    await group_manager.dissolve_group("does-not-exist")


async def test_create_group_emits_event(group_manager, event_bus, leader, follower1):
    """Creating a group should emit ZONE_GROUPED event."""
    events = []
    event_bus.on(EventType.ZONE_GROUPED, lambda e: events.append(e))

    group = await group_manager.create_group(leader, [follower1])
    assert len(events) == 1
    assert events[0].data["group_id"] == group.group_id
    assert leader.zone_id in events[0].data["zone_ids"]
    assert follower1.zone_id in events[0].data["zone_ids"]


async def test_create_group_syncs_playing_leader(group_manager, leader, follower1):
    """When leader is playing, creating a group should sync followers to the same track."""
    from tune_server.models import Track

    track = Track(title="Playing Track", track_number=1)
    leader.player.state = PlaybackState.PLAYING
    leader.player.current_track = track
    leader.position_ms = 5000

    group = await group_manager.create_group(leader, [follower1])

    # Follower should have had its queue set and track started
    follower1.player.queue.set_tracks.assert_called_once_with([track])
    follower1.player._start_track.assert_awaited_once()

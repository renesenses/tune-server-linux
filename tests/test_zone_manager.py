from __future__ import annotations

from unittest.mock import AsyncMock, PropertyMock

import pytest

from tune_server.audio.formats import LOCAL_CAPABILITIES
from tune_server.event_bus import EventType
from tune_server.models import OutputType
from tune_server.zones.zone import ZoneInstance


async def test_create_zone(zone_manager):
    zone = await zone_manager.create_zone("Living Room", OutputType.LOCAL)
    assert isinstance(zone, ZoneInstance)
    assert zone.name == "Living Room"
    assert zone.output_type == OutputType.LOCAL


async def test_create_zone_emits_event(zone_manager, event_bus):
    events = []
    event_bus.on(EventType.ZONE_CREATED, lambda e: events.append(e))

    await zone_manager.create_zone("Kitchen", OutputType.LOCAL)

    assert len(events) == 1
    assert events[0].data["name"] == "Kitchen"


async def test_create_zone_no_output_raises(zone_manager):
    # DLNA has no factory registered
    with pytest.raises(RuntimeError, match="Could not create output"):
        await zone_manager.create_zone("Test", OutputType.DLNA)


async def test_get_zone(zone_manager):
    zone = await zone_manager.create_zone("Room", OutputType.LOCAL)
    found = zone_manager.get_zone(zone.zone_id)
    assert found is not None
    assert found.zone_id == zone.zone_id


async def test_get_zone_missing(zone_manager):
    assert zone_manager.get_zone(999) is None


async def test_list_zones(zone_manager):
    await zone_manager.create_zone("Room A", OutputType.LOCAL)
    await zone_manager.create_zone("Room B", OutputType.LOCAL)
    zones = zone_manager.list_zones()
    assert len(zones) == 2


async def test_delete_zone(zone_manager, event_bus):
    zone = await zone_manager.create_zone("Room", OutputType.LOCAL)
    zid = zone.zone_id

    events = []
    event_bus.on(EventType.ZONE_DELETED, lambda e: events.append(e))

    await zone_manager.delete_zone(zid)

    assert zone_manager.get_zone(zid) is None
    assert len(events) == 1


async def test_delete_zone_cleanup(zone_manager):
    zone = await zone_manager.create_zone("Room", OutputType.LOCAL)
    zid = zone.zone_id
    await zone_manager.delete_zone(zid)
    # Zone should have been cleaned up (no error means cleanup ran)


async def test_register_output_factory(zone_manager):
    created = []

    async def custom_factory(device_id):
        output = AsyncMock()
        output.name = "custom"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        created.append(output)
        return output

    zone_manager.register_output_factory(OutputType.DLNA, custom_factory)
    zone = await zone_manager.create_zone("DLNA Zone", OutputType.DLNA)
    assert len(created) == 1
    assert zone.output_type == OutputType.DLNA


async def test_initialize_loads_from_db(db, event_bus):
    from tune_server.zones.manager import ZoneManager

    zm1 = ZoneManager(db, event_bus)

    async def _factory(device_id):
        output = AsyncMock()
        output.name = "mock"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        return output

    zm1.register_output_factory(OutputType.LOCAL, _factory)
    zone = await zm1.create_zone("Persisted", OutputType.LOCAL)
    zid = zone.zone_id

    # Create a new manager and initialize from DB
    zm2 = ZoneManager(db, event_bus)
    zm2.register_output_factory(OutputType.LOCAL, _factory)
    await zm2.initialize()

    assert zm2.get_zone(zid) is not None
    assert zm2.get_zone(zid).name == "Persisted"


# ---------------------------------------------------------------------------
# Hot-unplug: device goes offline, zone auto-pauses
# ---------------------------------------------------------------------------


async def test_device_lost_auto_pauses_playing_zone(db, event_bus):
    """When a device goes offline, any zone using it should auto-pause."""
    from tune_server.event_bus import Event
    from tune_server.models import PlaybackState
    from tune_server.zones.manager import ZoneManager

    zm = ZoneManager(db, event_bus)

    mock_output = AsyncMock()
    mock_output.name = "dlna-device"
    type(mock_output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
    type(mock_output).is_available = PropertyMock(return_value=True)

    async def _factory(device_id):
        return mock_output

    zm.register_output_factory(OutputType.DLNA, _factory)
    await zm.initialize()

    zone = await zm.create_zone("DLNA Room", OutputType.DLNA, output_device_id="dev-123")

    # Simulate zone is playing
    zone.player._state = PlaybackState.PLAYING
    zone.player.pause = AsyncMock()

    # Emit device lost
    await zm._on_device_lost(Event(
        type=EventType.DEVICE_LOST,
        data={"id": "dev-123"},
        source="discovery",
    ))

    assert zone.online is False
    zone.player.pause.assert_awaited_once()


async def test_device_lost_does_not_pause_stopped_zone(db, event_bus):
    """When a device goes offline but zone is stopped, no pause is called."""
    from tune_server.event_bus import Event
    from tune_server.models import PlaybackState
    from tune_server.zones.manager import ZoneManager

    zm = ZoneManager(db, event_bus)

    async def _factory(device_id):
        output = AsyncMock()
        output.name = "dlna"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        return output

    zm.register_output_factory(OutputType.DLNA, _factory)
    await zm.initialize()

    zone = await zm.create_zone("DLNA Room", OutputType.DLNA, output_device_id="dev-456")
    zone.player._state = PlaybackState.STOPPED
    zone.player.pause = AsyncMock()

    await zm._on_device_lost(Event(
        type=EventType.DEVICE_LOST,
        data={"id": "dev-456"},
        source="discovery",
    ))

    assert zone.online is False
    zone.player.pause.assert_not_awaited()


async def test_device_lost_ignores_unrelated_device(db, event_bus):
    """Device lost for an unrelated device_id should not affect the zone."""
    from tune_server.event_bus import Event
    from tune_server.zones.manager import ZoneManager

    zm = ZoneManager(db, event_bus)

    async def _factory(device_id):
        output = AsyncMock()
        output.name = "dlna"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        return output

    zm.register_output_factory(OutputType.DLNA, _factory)
    await zm.initialize()

    zone = await zm.create_zone("DLNA Room", OutputType.DLNA, output_device_id="dev-AAA")

    await zm._on_device_lost(Event(
        type=EventType.DEVICE_LOST,
        data={"id": "dev-BBB"},
        source="discovery",
    ))

    assert zone.online is True


# ---------------------------------------------------------------------------
# Hot-replug: device comes back, zone auto-resumes
# ---------------------------------------------------------------------------


async def test_device_discovered_marks_zone_online(db, event_bus):
    """When a lost device comes back, the zone should be marked online."""
    from unittest.mock import patch
    from tune_server.event_bus import Event
    from tune_server.models import PlaybackState
    from tune_server.zones.manager import ZoneManager

    zm = ZoneManager(db, event_bus)

    async def _factory(device_id):
        output = AsyncMock()
        output.name = "dlna"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        return output

    zm.register_output_factory(OutputType.DLNA, _factory)
    await zm.initialize()

    zone = await zm.create_zone("DLNA Room", OutputType.DLNA, output_device_id="dev-789")
    zone.player._state = PlaybackState.STOPPED
    zone.online = False

    with patch.object(zm, "_resume_zone_with_retry", new_callable=AsyncMock):
        await zm._on_device_discovered(Event(
            type=EventType.DEVICE_DISCOVERED,
            data={"id": "dev-789"},
            source="discovery",
        ))

    assert zone.online is True


async def test_device_discovered_triggers_resume_if_was_playing(db, event_bus):
    """When a device comes back and was_playing=True, resume should be triggered."""
    from unittest.mock import patch
    from tune_server.event_bus import Event
    from tune_server.models import PlaybackState
    from tune_server.zones.manager import ZoneManager

    zm = ZoneManager(db, event_bus)

    async def _factory(device_id):
        output = AsyncMock()
        output.name = "dlna"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        return output

    zm.register_output_factory(OutputType.DLNA, _factory)
    await zm.initialize()

    zone = await zm.create_zone("DLNA Room", OutputType.DLNA, output_device_id="dev-resume")

    # First, simulate device going offline while playing
    zone.player._state = PlaybackState.PLAYING
    zone.player.pause = AsyncMock()
    await zm._on_device_lost(Event(
        type=EventType.DEVICE_LOST,
        data={"id": "dev-resume"},
        source="discovery",
    ))
    assert zone.online is False

    # Now device comes back — _resume_zone_with_retry should be called
    with patch.object(zm, "_resume_zone_with_retry", new_callable=AsyncMock) as mock_resume:
        await zm._on_device_discovered(Event(
            type=EventType.DEVICE_DISCOVERED,
            data={"id": "dev-resume"},
            source="discovery",
        ))

    assert zone.online is True
    mock_resume.assert_called_once_with(zone)


async def test_device_discovered_no_resume_if_was_stopped(db, event_bus):
    """When a device comes back but zone was stopped, no resume should occur."""
    from unittest.mock import patch
    from tune_server.event_bus import Event
    from tune_server.models import PlaybackState
    from tune_server.zones.manager import ZoneManager

    zm = ZoneManager(db, event_bus)

    async def _factory(device_id):
        output = AsyncMock()
        output.name = "dlna"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        return output

    zm.register_output_factory(OutputType.DLNA, _factory)
    await zm.initialize()

    zone = await zm.create_zone("DLNA Room", OutputType.DLNA, output_device_id="dev-noresume")

    # Simulate offline while stopped
    zone.player._state = PlaybackState.STOPPED
    zone.player.pause = AsyncMock()
    await zm._on_device_lost(Event(
        type=EventType.DEVICE_LOST,
        data={"id": "dev-noresume"},
        source="discovery",
    ))

    with patch.object(zm, "_resume_zone_with_retry", new_callable=AsyncMock) as mock_resume:
        await zm._on_device_discovered(Event(
            type=EventType.DEVICE_DISCOVERED,
            data={"id": "dev-noresume"},
            source="discovery",
        ))

    assert zone.online is True
    mock_resume.assert_not_called()


# ---------------------------------------------------------------------------
# Zone creation with unknown output type
# ---------------------------------------------------------------------------


async def test_create_zone_unknown_output_type_raises(zone_manager):
    """Creating a zone with an unregistered output type (AIRPLAY) should raise."""
    with pytest.raises(RuntimeError, match="Could not create output"):
        await zone_manager.create_zone("AirPlay Zone", OutputType.AIRPLAY)


# ---------------------------------------------------------------------------
# Zone deletion while playing
# ---------------------------------------------------------------------------


async def test_delete_zone_while_playing(zone_manager, event_bus):
    """Deleting a zone that is currently playing should clean it up gracefully."""
    zone = await zone_manager.create_zone("Playing Room", OutputType.LOCAL)
    from tune_server.models import PlaybackState
    zone.player._state = PlaybackState.PLAYING

    events = []
    event_bus.on(EventType.ZONE_DELETED, lambda e: events.append(e))

    zid = zone.zone_id
    await zone_manager.delete_zone(zid)

    assert zone_manager.get_zone(zid) is None
    assert len(events) == 1


async def test_delete_nonexistent_zone_does_not_crash(zone_manager):
    """Deleting a zone that does not exist should not raise."""
    await zone_manager.delete_zone(999)


# ---------------------------------------------------------------------------
# set_output hot-swap
# ---------------------------------------------------------------------------


async def test_set_output_swaps_output_type(db, event_bus):
    """set_output should swap the output target on a zone."""
    from tune_server.zones.manager import ZoneManager

    zm = ZoneManager(db, event_bus)

    async def _local_factory(device_id):
        output = AsyncMock()
        output.name = "local"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        output.close = AsyncMock()
        return output

    async def _dlna_factory(device_id):
        output = AsyncMock()
        output.name = "dlna-out"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        output.close = AsyncMock()
        return output

    zm.register_output_factory(OutputType.LOCAL, _local_factory)
    zm.register_output_factory(OutputType.DLNA, _dlna_factory)

    zone = await zm.create_zone("Swappable", OutputType.LOCAL)
    assert zone.output_type == OutputType.LOCAL

    events = []
    event_bus.on(EventType.ZONE_UPDATED, lambda e: events.append(e))

    updated = await zm.set_output(zone.zone_id, OutputType.DLNA, "dlna-dev-1")
    assert updated.output_type == OutputType.DLNA
    assert updated.output_device_id == "dlna-dev-1"
    assert len(events) == 1
    assert events[0].data["output_type"] == "dlna"


async def test_set_output_noop_same_target(zone_manager):
    """set_output with same type and device_id should be a no-op."""
    zone = await zone_manager.create_zone("Room", OutputType.LOCAL)
    original_output = zone.output

    result = await zone_manager.set_output(zone.zone_id, OutputType.LOCAL, None)
    assert result.output is original_output


async def test_set_output_unknown_zone_raises(zone_manager):
    """set_output on a non-existent zone should raise KeyError."""
    with pytest.raises(KeyError, match="not found"):
        await zone_manager.set_output(999, OutputType.LOCAL)


async def test_set_output_duplicate_device_raises(db, event_bus):
    """set_output should reject swapping to a device already used by another zone."""
    from tune_server.zones.manager import ZoneManager

    zm = ZoneManager(db, event_bus)

    async def _factory(device_id):
        output = AsyncMock()
        output.name = f"out-{device_id}"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        output.close = AsyncMock()
        return output

    zm.register_output_factory(OutputType.DLNA, _factory)
    zm.register_output_factory(OutputType.LOCAL, _factory)

    await zm.create_zone("Zone A", OutputType.DLNA, output_device_id="dev-X")
    zone_b = await zm.create_zone("Zone B", OutputType.LOCAL)

    with pytest.raises(ValueError, match="already in use"):
        await zm.set_output(zone_b.zone_id, OutputType.DLNA, "dev-X")


# ---------------------------------------------------------------------------
# Duplicate device prevention
# ---------------------------------------------------------------------------


async def test_create_zone_duplicate_device_raises(db, event_bus):
    """Creating two zones pointing to the same device should raise ValueError."""
    from tune_server.zones.manager import ZoneManager

    zm = ZoneManager(db, event_bus)

    async def _factory(device_id):
        output = AsyncMock()
        output.name = f"out-{device_id}"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        return output

    zm.register_output_factory(OutputType.DLNA, _factory)

    await zm.create_zone("Zone 1", OutputType.DLNA, output_device_id="unique-dev")

    with pytest.raises(ValueError, match="already in use"):
        await zm.create_zone("Zone 2", OutputType.DLNA, output_device_id="unique-dev")


async def test_create_zone_same_type_different_device_ok(db, event_bus):
    """Two zones with the same output type but different devices should work."""
    from tune_server.zones.manager import ZoneManager

    zm = ZoneManager(db, event_bus)

    async def _factory(device_id):
        output = AsyncMock()
        output.name = f"out-{device_id}"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        return output

    zm.register_output_factory(OutputType.DLNA, _factory)

    z1 = await zm.create_zone("Zone 1", OutputType.DLNA, output_device_id="dev-A")
    z2 = await zm.create_zone("Zone 2", OutputType.DLNA, output_device_id="dev-B")

    assert z1.output_device_id == "dev-A"
    assert z2.output_device_id == "dev-B"


async def test_create_zone_no_device_id_allows_multiple(zone_manager):
    """Multiple LOCAL zones without explicit device_id should be allowed."""
    z1 = await zone_manager.create_zone("Local 1", OutputType.LOCAL)
    z2 = await zone_manager.create_zone("Local 2", OutputType.LOCAL)
    assert z1.zone_id != z2.zone_id


# ---------------------------------------------------------------------------
# retry_pending_zones mechanism
# ---------------------------------------------------------------------------


async def test_retry_pending_zones_loads_when_output_available(db, event_bus):
    """Pending zones should be loaded when their output becomes available."""
    from tune_server.zones.manager import ZoneManager

    zm = ZoneManager(db, event_bus)

    # No factory registered yet — simulate startup with unavailable output
    call_count = 0

    async def _sometimes_factory(device_id):
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            # First call returns None (device not available)
            return None
        output = AsyncMock()
        output.name = "dlna-delayed"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        return output

    zm.register_output_factory(OutputType.DLNA, _sometimes_factory)

    # Manually insert a zone row into the DB that references DLNA
    zone_id = await zm._zone_repo.create("Pending Zone", "dlna", "dlna-dev-99")

    # Initialize — first attempt will fail (factory returns None)
    await zm.initialize()
    assert zm.get_zone(zone_id) is None
    assert len(zm._pending_zones) == 1

    # Retry — second attempt should succeed
    await zm.retry_pending_zones()
    assert zm.get_zone(zone_id) is not None
    assert zm.get_zone(zone_id).name == "Pending Zone"
    assert len(zm._pending_zones) == 0


async def test_retry_pending_zones_noop_when_empty(zone_manager):
    """retry_pending_zones with no pending zones should be a no-op."""
    assert len(zone_manager._pending_zones) == 0
    await zone_manager.retry_pending_zones()  # Should not raise


async def test_retry_pending_zones_keeps_still_unavailable(db, event_bus):
    """Zones whose output is still unavailable remain in pending list."""
    from tune_server.zones.manager import ZoneManager

    zm = ZoneManager(db, event_bus)

    async def _null_factory(device_id):
        return None

    zm.register_output_factory(OutputType.DLNA, _null_factory)

    zone_id = await zm._zone_repo.create("Still Pending", "dlna", "missing-dev")

    await zm.initialize()
    assert len(zm._pending_zones) == 1

    await zm.retry_pending_zones()
    assert len(zm._pending_zones) == 1
    assert zm.get_zone(zone_id) is None

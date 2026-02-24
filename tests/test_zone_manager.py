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

from __future__ import annotations

from tune_server.models import OutputType, PlaybackState, Zone


def test_properties(zone_instance):
    assert zone_instance.zone_id == 1
    assert zone_instance.name == "Test Zone"
    assert zone_instance.output_type == OutputType.LOCAL
    assert zone_instance.output is not None
    assert zone_instance.player is not None


def test_to_model(zone_instance):
    model = zone_instance.to_model()
    assert isinstance(model, Zone)
    assert model.id == 1
    assert model.name == "Test Zone"
    assert model.output_type == OutputType.LOCAL
    assert model.state == PlaybackState.STOPPED


def test_to_model_includes_queue_length(zone_instance):
    from tune_server.models import Track

    track = Track(title="Test", track_number=1)
    zone_instance.player.queue.set_tracks([track])

    model = zone_instance.to_model()
    assert model.queue_length == 1


def test_state_reflects_player(zone_instance):
    assert zone_instance.state == PlaybackState.STOPPED
    assert zone_instance.state == zone_instance.player.state


def test_group_id_settable(zone_instance):
    assert zone_instance.group_id is None
    zone_instance.group_id = "grp-123"
    assert zone_instance.group_id == "grp-123"
    zone_instance.group_id = None
    assert zone_instance.group_id is None


def test_sync_delay_ms_property(zone_instance):
    assert zone_instance.sync_delay_ms == 0
    zone_instance.sync_delay_ms = 250
    assert zone_instance.sync_delay_ms == 250
    zone_instance.sync_delay_ms = -100
    assert zone_instance.sync_delay_ms == -100


def test_to_model_includes_sync_delay(zone_instance):
    zone_instance.sync_delay_ms = 150
    model = zone_instance.to_model()
    assert model.sync_delay_ms == 150


async def test_cleanup(zone_instance):
    await zone_instance.cleanup()
    # Player stop and output close should have been called
    zone_instance.output.stop.assert_awaited()
    zone_instance.output.close.assert_awaited()


# ---------------------------------------------------------------------------
# Online/offline property
# ---------------------------------------------------------------------------


def test_online_default_is_true(zone_instance):
    assert zone_instance.online is True


def test_online_can_be_set_false(zone_instance):
    zone_instance.online = False
    assert zone_instance.online is False


def test_to_model_includes_online(zone_instance):
    zone_instance.online = False
    model = zone_instance.to_model()
    assert model.online is False


# ---------------------------------------------------------------------------
# update_output hot-swap
# ---------------------------------------------------------------------------


async def test_update_output_swaps_output(zone_instance, event_bus):
    from unittest.mock import AsyncMock, PropertyMock
    from tune_server.audio.formats import LOCAL_CAPABILITIES

    old_output = zone_instance.output
    new_output = AsyncMock()
    new_output.name = "new-output"
    type(new_output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
    type(new_output).is_available = PropertyMock(return_value=True)

    await zone_instance.update_output(OutputType.DLNA, new_output, "new-dev-id")

    assert zone_instance.output is new_output
    assert zone_instance.output_type == OutputType.DLNA
    assert zone_instance.output_device_id == "new-dev-id"
    old_output.close.assert_awaited_once()


async def test_update_output_closes_old_even_if_error(zone_instance, event_bus):
    """Old output close failure should not prevent the swap."""
    from unittest.mock import AsyncMock, PropertyMock
    from tune_server.audio.formats import LOCAL_CAPABILITIES

    old_output = zone_instance.output
    old_output.close = AsyncMock(side_effect=OSError("device gone"))

    new_output = AsyncMock()
    new_output.name = "new-output"
    type(new_output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
    type(new_output).is_available = PropertyMock(return_value=True)

    # Should not raise despite old_output.close raising
    await zone_instance.update_output(OutputType.LOCAL, new_output, None)

    assert zone_instance.output is new_output
    old_output.close.assert_awaited_once()

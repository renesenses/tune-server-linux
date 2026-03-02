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

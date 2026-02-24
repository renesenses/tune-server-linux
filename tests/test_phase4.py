"""Phase 4 tests: playback robustness & queue completeness."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import AudioFormat, PlaybackState, Source, Track
from tune_server.playback.queue import PlayQueue


# ===========================================================================
# S1: Playback Error Event Type
# ===========================================================================

class TestPlaybackErrorEvent:
    def test_playback_error_event_exists(self):
        """PLAYBACK_ERROR event type exists with correct value."""
        assert hasattr(EventType, "PLAYBACK_ERROR")
        assert EventType.PLAYBACK_ERROR.value == "playback.error"

    async def test_error_event_emitted_and_received(self):
        """Error event emitted through EventBus reaches listeners."""
        bus = EventBus()
        received = []

        async def listener(event):
            received.append(event)

        bus.on(EventType.PLAYBACK_ERROR, listener)

        await bus.emit(Event(
            type=EventType.PLAYBACK_ERROR,
            data={"zone_id": 1, "error": "test", "message": "test error"},
            source="test",
        ))

        assert len(received) == 1
        assert received[0].data["error"] == "test"

    async def test_error_event_received_by_global_listener(self):
        """Error events are received by on_all listeners (WebSocket broadcast)."""
        bus = EventBus()
        received = []

        async def listener(event):
            received.append(event)

        bus.on_all(listener)

        await bus.emit(Event(
            type=EventType.PLAYBACK_ERROR,
            data={"zone_id": 1, "error": "test"},
            source="player",
        ))

        assert len(received) == 1
        assert received[0].type == EventType.PLAYBACK_ERROR


# ===========================================================================
# S2: Playback Timeouts & Error Reporting
# ===========================================================================

class TestPlaybackTimeouts:
    def _make_player(self, event_bus=None):
        from tune_server.playback.player import Player
        bus = event_bus or EventBus()
        player = Player(zone_id=1, event_bus=bus)
        output = AsyncMock()
        type(output).capabilities = PropertyMock(
            return_value=MagicMock(max_sample_rate=192000, max_bit_depth=24,
                                   supported_formats=[AudioFormat.FLAC])
        )
        player.set_output(output)
        return player, bus

    async def test_stream_url_timeout_emits_error_and_advances(self):
        """Stream URL timeout emits PLAYBACK_ERROR and advances track."""
        player, bus = self._make_player()
        errors = []

        async def on_error(event):
            errors.append(event)

        bus.on(EventType.PLAYBACK_ERROR, on_error)

        track1 = Track(title="Tidal Song", source=Source.TIDAL, source_id="t123", duration_ms=180000)
        track2 = Track(title="Local Song", file_path="/music/local.flac",
                       format=AudioFormat.FLAC, duration_ms=200000)

        async def slow_resolver(track):
            await asyncio.sleep(100)  # Will timeout
            return "http://example.com/stream"

        player.set_stream_url_resolver(slow_resolver)
        player.queue.set_tracks([track1, track2], 0)

        with patch.object(player, '_advance_track', new_callable=AsyncMock) as mock_advance:
            with patch("tune_server.playback.player.settings") as mock_settings:
                mock_settings.stream_url_resolve_timeout = 0.1
                mock_settings.pipeline_start_timeout = 30
                await player._start_track(track1)

            mock_advance.assert_awaited_once()

        assert len(errors) == 1
        assert errors[0].data["error"] == "stream_url_timeout"
        assert "Tidal Song" in errors[0].data["message"]

    async def test_stream_url_failure_emits_error_and_advances(self):
        """Stream URL resolution failure emits PLAYBACK_ERROR and advances."""
        player, bus = self._make_player()
        errors = []

        async def on_error(event):
            errors.append(event)

        bus.on(EventType.PLAYBACK_ERROR, on_error)

        track = Track(title="Qobuz Song", source=Source.QOBUZ, source_id="q456", duration_ms=180000)

        async def failing_resolver(track):
            raise ConnectionError("Service unavailable")

        player.set_stream_url_resolver(failing_resolver)
        player.queue.set_tracks([track], 0)

        with patch.object(player, '_advance_track', new_callable=AsyncMock) as mock_advance:
            await player._start_track(track)
            mock_advance.assert_awaited_once()

        assert len(errors) == 1
        assert errors[0].data["error"] == "stream_url_failed"

    async def test_pipeline_timeout_emits_error_stops(self):
        """Pipeline timeout emits PLAYBACK_ERROR and sets state to STOPPED."""
        player, bus = self._make_player()
        errors = []

        async def on_error(event):
            errors.append(event)

        bus.on(EventType.PLAYBACK_ERROR, on_error)

        track = Track(title="Test", file_path="/music/test.flac",
                      format=AudioFormat.FLAC, duration_ms=200000)
        player.queue.set_tracks([track], 0)

        with patch("tune_server.playback.player.AudioPipeline") as MockPipeline:
            mock_pipeline = AsyncMock()

            async def slow_start(**kwargs):
                await asyncio.sleep(100)

            mock_pipeline.start = slow_start
            MockPipeline.return_value = mock_pipeline

            with patch("tune_server.playback.player.settings") as mock_settings:
                mock_settings.stream_url_resolve_timeout = 15
                mock_settings.pipeline_start_timeout = 0.1
                await player._start_track(track)

        assert player.state == PlaybackState.STOPPED
        assert len(errors) == 1
        assert errors[0].data["error"] == "pipeline_error"

    async def test_output_start_error_emits_error(self):
        """Output start error emits PLAYBACK_ERROR."""
        player, bus = self._make_player()
        errors = []

        async def on_error(event):
            errors.append(event)

        bus.on(EventType.PLAYBACK_ERROR, on_error)

        track = Track(title="Test", file_path="/music/test.flac",
                      format=AudioFormat.FLAC, duration_ms=200000)
        player.queue.set_tracks([track], 0)

        with patch("tune_server.playback.player.AudioPipeline") as MockPipeline:
            mock_pipeline = AsyncMock()
            mock_pipeline.start = AsyncMock(return_value=MagicMock())
            MockPipeline.return_value = mock_pipeline

            player._output.start = AsyncMock(side_effect=OSError("Device busy"))

            with patch("tune_server.playback.player.settings") as mock_settings:
                mock_settings.stream_url_resolve_timeout = 15
                mock_settings.pipeline_start_timeout = 30
                await player._start_track(track)

        assert player.state == PlaybackState.STOPPED
        assert len(errors) == 1
        assert errors[0].data["error"] == "output_error"


class TestSeekValidation:
    def _make_player(self):
        from tune_server.playback.player import Player
        bus = EventBus()
        player = Player(zone_id=1, event_bus=bus)
        output = AsyncMock()
        type(output).capabilities = PropertyMock(
            return_value=MagicMock(max_sample_rate=192000, max_bit_depth=24,
                                   supported_formats=[AudioFormat.FLAC])
        )
        player.set_output(output)
        return player

    async def test_seek_negative_clamped_to_zero(self):
        """Seeking to negative position is clamped to 0."""
        player = self._make_player()
        track = Track(title="Test", file_path="/music/test.flac",
                      format=AudioFormat.FLAC, duration_ms=200000)
        player.queue.set_tracks([track], 0)
        player._state = PlaybackState.PLAYING

        with patch.object(player, '_stop_pipeline', new_callable=AsyncMock):
            with patch.object(player, '_start_track', new_callable=AsyncMock) as mock_start:
                await player.seek(-5000)
                mock_start.assert_awaited_once_with(track, seek_ms=0)

    async def test_seek_past_end_clamped_to_duration(self):
        """Seeking past track duration is clamped to duration_ms."""
        player = self._make_player()
        track = Track(title="Test", file_path="/music/test.flac",
                      format=AudioFormat.FLAC, duration_ms=200000)
        player.queue.set_tracks([track], 0)
        player._state = PlaybackState.PLAYING

        with patch.object(player, '_stop_pipeline', new_callable=AsyncMock):
            with patch.object(player, '_start_track', new_callable=AsyncMock) as mock_start:
                await player.seek(999999)
                mock_start.assert_awaited_once_with(track, seek_ms=200000)

    async def test_seek_within_range_unchanged(self):
        """Seeking within valid range passes position unchanged."""
        player = self._make_player()
        track = Track(title="Test", file_path="/music/test.flac",
                      format=AudioFormat.FLAC, duration_ms=200000)
        player.queue.set_tracks([track], 0)
        player._state = PlaybackState.PLAYING

        with patch.object(player, '_stop_pipeline', new_callable=AsyncMock):
            with patch.object(player, '_start_track', new_callable=AsyncMock) as mock_start:
                await player.seek(100000)
                mock_start.assert_awaited_once_with(track, seek_ms=100000)


class TestTimeoutConfigDefaults:
    def test_stream_url_resolve_timeout_default(self):
        from tune_server.config import Settings
        s = Settings()
        assert s.stream_url_resolve_timeout == 15

    def test_pipeline_start_timeout_default(self):
        from tune_server.config import Settings
        s = Settings()
        assert s.pipeline_start_timeout == 30


# ===========================================================================
# S3: Streaming Queue Persistence
# ===========================================================================

class TestStreamingQueuePersistence:
    async def test_save_queue_with_streaming_tracks_as_json(self, db, event_bus, mock_output):
        """Queue with streaming tracks is saved as JSON in zones table."""
        from tune_server.db.repository import PlayQueueRepo, ZoneRepo
        from tune_server.zones.zone import ZoneInstance

        zone_repo = ZoneRepo(db)
        queue_repo = PlayQueueRepo(db)
        zone_id = await zone_repo.create("Test Zone", "local")

        zi = ZoneInstance(
            zone_id=zone_id, name="Test Zone",
            output_type="local", output=mock_output,
            event_bus=event_bus, queue_repo=queue_repo,
            zone_repo=zone_repo,
        )

        tracks = [
            Track(title="Tidal Track", source=Source.TIDAL, source_id="t123",
                  duration_ms=180000, format=AudioFormat.FLAC),
            Track(title="Qobuz Track", source=Source.QOBUZ, source_id="q456",
                  duration_ms=200000, format=AudioFormat.FLAC),
        ]

        await zi._save_queue(tracks, 0)

        row = await zone_repo.get(zone_id)
        assert row["queue_json"] is not None
        data = json.loads(row["queue_json"])
        assert len(data["tracks"]) == 2
        assert data["tracks"][0]["source"] == "tidal"
        assert data["tracks"][0]["source_id"] == "t123"
        assert data["position"] == 0

    async def test_streaming_track_file_path_not_persisted(self, db, event_bus, mock_output):
        """Streaming track file_path (URL) is NOT persisted since URLs expire."""
        from tune_server.db.repository import PlayQueueRepo, ZoneRepo
        from tune_server.zones.zone import ZoneInstance

        zone_repo = ZoneRepo(db)
        queue_repo = PlayQueueRepo(db)
        zone_id = await zone_repo.create("Test Zone", "local")

        zi = ZoneInstance(
            zone_id=zone_id, name="Test Zone",
            output_type="local", output=mock_output,
            event_bus=event_bus, queue_repo=queue_repo,
            zone_repo=zone_repo,
        )

        tracks = [
            Track(title="Tidal Track", source=Source.TIDAL, source_id="t123",
                  file_path="https://cdn.tidal.com/stream/expire-soon",
                  duration_ms=180000, format=AudioFormat.FLAC),
        ]

        await zi._save_queue(tracks, 0)

        row = await zone_repo.get(zone_id)
        data = json.loads(row["queue_json"])
        # file_path for streaming tracks should be None
        assert data["tracks"][0]["file_path"] is None
        assert data["tracks"][0]["source_id"] == "t123"

    async def test_restore_queue_from_json(self, db, event_bus, mock_output):
        """Queue restored from JSON includes streaming tracks."""
        from tune_server.db.repository import PlayQueueRepo, ZoneRepo
        from tune_server.zones.zone import ZoneInstance

        zone_repo = ZoneRepo(db)
        queue_repo = PlayQueueRepo(db)
        zone_id = await zone_repo.create("Test Zone", "local")

        zi = ZoneInstance(
            zone_id=zone_id, name="Test Zone",
            output_type="local", output=mock_output,
            event_bus=event_bus, queue_repo=queue_repo,
            zone_repo=zone_repo,
        )

        tracks = [
            Track(title="Tidal Track", source=Source.TIDAL, source_id="t123",
                  duration_ms=180000, format=AudioFormat.FLAC),
            Track(title="Qobuz Track", source=Source.QOBUZ, source_id="q456",
                  duration_ms=200000, format=AudioFormat.FLAC),
        ]

        await zi._save_queue(tracks, 1)

        # Create a fresh instance and restore
        zi2 = ZoneInstance(
            zone_id=zone_id, name="Test Zone",
            output_type="local", output=mock_output,
            event_bus=event_bus, queue_repo=queue_repo,
            zone_repo=zone_repo,
        )
        await zi2.restore_queue()

        assert zi2.player.queue.length == 2
        assert zi2.player.queue.position == 1
        restored = zi2.player.queue.tracks
        assert restored[0].title == "Tidal Track"
        assert restored[0].source == Source.TIDAL
        assert restored[0].source_id == "t123"
        assert restored[0].file_path is None  # Streaming URL not persisted
        assert restored[1].title == "Qobuz Track"
        assert restored[1].source == Source.QOBUZ

    async def test_mixed_queue_roundtrip(self, db, event_bus, mock_output, sample_tracks):
        """Mixed queue (local + streaming) round-trips correctly."""
        from tune_server.db.repository import PlayQueueRepo, ZoneRepo
        from tune_server.zones.zone import ZoneInstance

        zone_repo = ZoneRepo(db)
        queue_repo = PlayQueueRepo(db)
        zone_id = await zone_repo.create("Test Zone", "local")

        zi = ZoneInstance(
            zone_id=zone_id, name="Test Zone",
            output_type="local", output=mock_output,
            event_bus=event_bus, queue_repo=queue_repo,
            zone_repo=zone_repo,
        )

        mixed_tracks = [
            sample_tracks[0],  # Local track with DB id
            Track(title="Tidal Track", source=Source.TIDAL, source_id="t123",
                  duration_ms=180000, format=AudioFormat.FLAC),
            sample_tracks[1],  # Local track with DB id
        ]

        await zi._save_queue(mixed_tracks, 0)

        zi2 = ZoneInstance(
            zone_id=zone_id, name="Test Zone",
            output_type="local", output=mock_output,
            event_bus=event_bus, queue_repo=queue_repo,
            zone_repo=zone_repo,
        )
        await zi2.restore_queue()

        assert zi2.player.queue.length == 3
        restored = zi2.player.queue.tracks
        assert restored[0].title == sample_tracks[0].title
        assert restored[0].source == Source.LOCAL
        assert restored[1].title == "Tidal Track"
        assert restored[1].source == Source.TIDAL
        assert restored[2].title == sample_tracks[1].title

    async def test_fallback_to_play_queue_table(self, db, event_bus, mock_output, sample_tracks):
        """Falls back to play_queue table when no queue_json."""
        from tune_server.db.repository import PlayQueueRepo, ZoneRepo
        from tune_server.zones.zone import ZoneInstance

        zone_repo = ZoneRepo(db)
        queue_repo = PlayQueueRepo(db)
        zone_id = await zone_repo.create("Test Zone", "local")

        # Write directly to play_queue table (old format)
        track_ids = [t.id for t in sample_tracks]
        await queue_repo.set_queue(zone_id, track_ids)
        await queue_repo.set_current(zone_id, 1)

        zi = ZoneInstance(
            zone_id=zone_id, name="Test Zone",
            output_type="local", output=mock_output,
            event_bus=event_bus, queue_repo=queue_repo,
            zone_repo=zone_repo,
        )
        await zi.restore_queue()

        assert zi.player.queue.length == len(sample_tracks)
        assert zi.player.queue.position == 1


# ===========================================================================
# S4: Queue API Completeness
# ===========================================================================

class TestQueueMoveTrack:
    def test_move_track_forward(self):
        """move_track moves a track to a later position."""
        q = PlayQueue()
        tracks = [Track(title=f"Track {i}") for i in range(5)]
        q.set_tracks(tracks, 0)

        assert q.move_track(0, 3)
        assert q.tracks[0].title == "Track 1"
        assert q.tracks[3].title == "Track 0"

    def test_move_track_backward(self):
        """move_track moves a track to an earlier position."""
        q = PlayQueue()
        tracks = [Track(title=f"Track {i}") for i in range(5)]
        q.set_tracks(tracks, 4)

        assert q.move_track(3, 1)
        assert q.tracks[1].title == "Track 3"
        assert q.tracks[2].title == "Track 1"

    def test_move_track_adjusts_current_position(self):
        """Moving the current track updates _position."""
        q = PlayQueue()
        tracks = [Track(title=f"Track {i}") for i in range(5)]
        q.set_tracks(tracks, 1)  # Track 1 is current

        assert q.move_track(1, 3)
        assert q.position == 3
        assert q.current.title == "Track 1"

    def test_move_track_adjusts_position_when_moving_before_current(self):
        """Moving a track from before current to after current adjusts position."""
        q = PlayQueue()
        tracks = [Track(title=f"Track {i}") for i in range(5)]
        q.set_tracks(tracks, 2)

        assert q.move_track(0, 3)
        assert q.position == 1  # Shifted down by 1

    def test_move_track_adjusts_position_when_moving_after_to_before_current(self):
        """Moving a track from after current to before current adjusts position."""
        q = PlayQueue()
        tracks = [Track(title=f"Track {i}") for i in range(5)]
        q.set_tracks(tracks, 2)

        assert q.move_track(4, 1)
        assert q.position == 3  # Shifted up by 1

    def test_move_track_same_position(self):
        """Moving to same position returns True (no-op)."""
        q = PlayQueue()
        tracks = [Track(title=f"Track {i}") for i in range(3)]
        q.set_tracks(tracks, 0)

        assert q.move_track(1, 1)
        assert q.tracks[1].title == "Track 1"

    def test_move_track_out_of_bounds_returns_false(self):
        """Out of bounds positions return False."""
        q = PlayQueue()
        tracks = [Track(title=f"Track {i}") for i in range(3)]
        q.set_tracks(tracks, 0)

        assert not q.move_track(-1, 1)
        assert not q.move_track(0, 5)
        assert not q.move_track(10, 0)

    def test_move_track_empty_queue_returns_false(self):
        """Moving in an empty queue returns False."""
        q = PlayQueue()
        assert not q.move_track(0, 1)


class TestQueueAPIEndpoints:
    async def test_delete_queue_item(self, app_client_with_zones):
        client = app_client_with_zones
        # Create a zone
        resp = await client.post("/api/v1/zones", json={"name": "Test Zone"})
        zone_id = resp.json()["id"]
        zone = deps_get_zone(zone_id)

        tracks = [Track(title=f"Track {i}", file_path=f"/music/{i}.flac") for i in range(3)]
        zone.player.queue.set_tracks(tracks, 0)

        resp = await client.delete(f"/api/v1/zones/{zone_id}/queue/1")
        assert resp.status_code == 200
        assert resp.json()["queue_length"] == 2

    async def test_delete_queue_item_invalid_index(self, app_client_with_zones):
        client = app_client_with_zones
        resp = await client.post("/api/v1/zones", json={"name": "Test Zone"})
        zone_id = resp.json()["id"]

        resp = await client.delete(f"/api/v1/zones/{zone_id}/queue/99")
        assert resp.status_code == 400

    async def test_post_queue_move(self, app_client_with_zones):
        client = app_client_with_zones
        resp = await client.post("/api/v1/zones", json={"name": "Test Zone"})
        zone_id = resp.json()["id"]
        zone = deps_get_zone(zone_id)

        tracks = [Track(title=f"Track {i}", file_path=f"/music/{i}.flac") for i in range(4)]
        zone.player.queue.set_tracks(tracks, 0)

        resp = await client.post(
            f"/api/v1/zones/{zone_id}/queue/move",
            json={"from_position": 0, "to_position": 2},
        )
        assert resp.status_code == 200
        assert resp.json()["queue_length"] == 4

    async def test_post_queue_move_invalid(self, app_client_with_zones):
        client = app_client_with_zones
        resp = await client.post("/api/v1/zones", json={"name": "Test Zone"})
        zone_id = resp.json()["id"]

        resp = await client.post(
            f"/api/v1/zones/{zone_id}/queue/move",
            json={"from_position": 0, "to_position": 99},
        )
        assert resp.status_code == 400

    async def test_post_queue_jump(self, app_client_with_zones):
        client = app_client_with_zones
        resp = await client.post("/api/v1/zones", json={"name": "Test Zone"})
        zone_id = resp.json()["id"]
        zone = deps_get_zone(zone_id)

        tracks = [Track(title=f"Track {i}", file_path=f"/music/{i}.flac") for i in range(4)]
        zone.player.queue.set_tracks(tracks, 0)

        resp = await client.post(
            f"/api/v1/zones/{zone_id}/queue/jump",
            json={"position": 2},
        )
        assert resp.status_code == 200

    async def test_post_queue_jump_invalid(self, app_client_with_zones):
        client = app_client_with_zones
        resp = await client.post("/api/v1/zones", json={"name": "Test Zone"})
        zone_id = resp.json()["id"]

        resp = await client.post(
            f"/api/v1/zones/{zone_id}/queue/jump",
            json={"position": 99},
        )
        assert resp.status_code == 400


# ===========================================================================
# S5: Configurable Watcher Debounce
# ===========================================================================

class TestWatcherDebounce:
    def test_default_debounce_is_two_seconds(self):
        from tune_server.config import Settings
        s = Settings()
        assert s.watcher_debounce_seconds == 2.0

    def test_debounce_configurable(self):
        from tune_server.config import Settings
        s = Settings(watcher_debounce_seconds=5.0)
        assert s.watcher_debounce_seconds == 5.0

    def test_watcher_uses_setting(self):
        """Watcher code references settings.watcher_debounce_seconds."""
        import inspect
        from tune_server.library.watcher import FileSystemWatcher
        source = inspect.getsource(FileSystemWatcher)
        assert "settings.watcher_debounce_seconds" in source
        assert "sleep(2.0)" not in source


# ===========================================================================
# Helpers
# ===========================================================================

def deps_get_zone(zone_id: int):
    """Helper to get zone from deps."""
    from tune_server.api.deps import deps
    return deps.zone_manager.get_zone(zone_id)

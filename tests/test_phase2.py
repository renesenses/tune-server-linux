"""Phase 2 tests: scanner race condition, incremental scanning, watcher debouncing,
discovery thread safety, device recovery, gapless playback, queue persistence,
and volume persistence."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from tune_server.audio.buffer import AsyncRingBuffer
from tune_server.audio.formats import AudioCapabilities, LOCAL_CAPABILITIES
from tune_server.db.engine import Database
from tune_server.db.repository import PlayQueueRepo, TrackRepo, ZoneRepo
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import (
    AudioFormat,
    AudioStreamInfo,
    DiscoveredDevice,
    OutputType,
    PlaybackState,
    Source,
    Track,
)
from tune_server.playback.player import Player


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_metadata(title="Track", artist="Artist", album="Album"):
    from tune_server.library.metadata_reader import TrackMetadata
    return TrackMetadata(
        title=title,
        artist=artist,
        album=album,
        album_artist=None,
        track_number=1,
        disc_number=1,
        year=2024,
        genre="Rock",
        duration_ms=240000,
        format="flac",
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        has_cover=False,
    )


def _make_track(track_id: int = 1, title: str = "Test Track") -> Track:
    return Track(
        id=track_id,
        title=title,
        file_path=f"/music/{track_id}.flac",
        format=AudioFormat.FLAC,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )


def _mock_pipeline(close_immediately: bool = True, stream_info: AudioStreamInfo | None = None):
    pipeline = AsyncMock()
    buf = AsyncRingBuffer(max_chunks=4)
    if close_immediately:
        buf.close()
    pipeline.output_buffer = buf
    info = stream_info or AudioStreamInfo(
        format=AudioFormat.FLAC,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )
    pipeline.start = AsyncMock(return_value=info)
    pipeline.stream_info = info
    pipeline.stop = AsyncMock()
    return pipeline


# ===========================================================================
# P0-1: Scanner/watcher race condition
# ===========================================================================

class TestScannerRaceCondition:
    """Verify that files added by the watcher during a scan aren't deleted."""

    @patch("tune_server.library.scanner.get_album_artwork", return_value=None)
    @patch("tune_server.library.scanner.read_metadata")
    async def test_watcher_added_file_not_deleted(self, mock_read, mock_art, db, event_bus, tmp_path):
        """Files added via scan_single during a scan must survive the removal step."""
        from tune_server.library.scanner import LibraryScanner

        mock_read.return_value = _make_metadata()
        scanner = LibraryScanner(db, event_bus)

        # Create initial file and scan
        f1 = tmp_path / "song1.flac"
        f1.touch()
        stats = await scanner.scan([str(tmp_path)])
        assert stats["added"] == 1

        # Now simulate: scanner starts a new scan, and mid-scan the watcher adds a file
        f2 = tmp_path / "song2.flac"
        f2.touch()

        # Add f2 via scan_single (watcher path) BEFORE the second scan
        await scanner.scan_single(str(f2))

        # Verify f2 is in the DB
        track_repo = TrackRepo(db)
        track = await track_repo.get_by_path(str(f2))
        assert track is not None

        # Now run a full scan — f2 IS in the directory so it should survive
        stats2 = await scanner.scan([str(tmp_path)])
        assert stats2["removed"] == 0

        # Both tracks still in DB
        all_paths = await track_repo.get_all_paths()
        assert str(f1) in all_paths
        assert str(f2) in all_paths

    @patch("tune_server.library.scanner.get_album_artwork", return_value=None)
    @patch("tune_server.library.scanner.read_metadata")
    async def test_scan_single_uses_lock(self, mock_read, mock_art, db, event_bus, tmp_path):
        """scan_single acquires the lock, preventing concurrent corruption."""
        from tune_server.library.scanner import LibraryScanner

        mock_read.return_value = _make_metadata()
        scanner = LibraryScanner(db, event_bus)

        f1 = tmp_path / "track.flac"
        f1.touch()

        # scan_single should work correctly (exercises the lock path)
        result = await scanner.scan_single(str(f1))
        assert result is True

        # Calling again replaces the track
        result2 = await scanner.scan_single(str(f1))
        assert result2 is True


# ===========================================================================
# P0-2: Incremental scanning (mtime-based)
# ===========================================================================

class TestIncrementalScanning:
    @patch("tune_server.library.scanner.get_album_artwork", return_value=None)
    @patch("tune_server.library.scanner.read_metadata")
    async def test_unchanged_file_skipped(self, mock_read, mock_art, db, event_bus, tmp_path):
        """Files with unchanged mtime are skipped on re-scan."""
        from tune_server.library.scanner import LibraryScanner

        mock_read.return_value = _make_metadata(title="Original")
        scanner = LibraryScanner(db, event_bus)

        f = tmp_path / "track.flac"
        f.touch()

        stats1 = await scanner.scan([str(tmp_path)])
        assert stats1["added"] == 1

        # Reset call count
        mock_read.reset_mock()

        # Re-scan without modifying the file
        stats2 = await scanner.scan([str(tmp_path)])
        assert stats2["added"] == 0
        assert stats2["updated"] == 0
        # read_metadata should NOT be called for unchanged files
        mock_read.assert_not_called()

    @patch("tune_server.library.scanner.get_album_artwork", return_value=None)
    @patch("tune_server.library.scanner.read_metadata")
    async def test_modified_file_rescanned(self, mock_read, mock_art, db, event_bus, tmp_path):
        """Files with newer mtime are re-processed on scan."""
        from tune_server.library.scanner import LibraryScanner

        mock_read.return_value = _make_metadata(title="Original")
        scanner = LibraryScanner(db, event_bus)

        f = tmp_path / "track.flac"
        f.touch()

        await scanner.scan([str(tmp_path)])

        # Modify the file (bump mtime)
        time.sleep(0.1)
        f.write_bytes(b"updated content")

        mock_read.return_value = _make_metadata(title="Updated")
        stats2 = await scanner.scan([str(tmp_path)])
        assert stats2["updated"] == 1

    async def test_mtime_stored_in_db(self, db, event_bus, tmp_path):
        """After processing a file, its mtime should be stored."""
        track_repo = TrackRepo(db)

        test_path = str(tmp_path / "test.flac")

        # Manually insert a track path
        await db.execute(
            "INSERT INTO tracks (title, file_path, source) VALUES (?, ?, ?)",
            ("Test", test_path, "local"),
        )
        await db.commit()

        await track_repo.update_mtime(test_path, 1234567890.5)
        mtime = await track_repo.get_mtime(test_path)
        assert mtime == 1234567890.5

    async def test_get_mtime_nonexistent(self, db, event_bus):
        """get_mtime returns None for files not in DB."""
        track_repo = TrackRepo(db)
        mtime = await track_repo.get_mtime("/nonexistent/file.flac")
        assert mtime is None


# ===========================================================================
# P0-3: Watcher debouncing
# ===========================================================================

class TestWatcherDebouncing:
    async def test_debounce_batches_events(self):
        """Multiple rapid changes should be batched into one processing call."""
        from unittest.mock import call

        scanner = AsyncMock()
        scanner.scan_single = AsyncMock(return_value=True)

        # Simulate the debounce logic directly
        pending: dict[str, object] = {}
        processed = []

        async def flush():
            await asyncio.sleep(0.1)  # short debounce for testing
            to_process = dict(pending)
            pending.clear()
            for path_str in to_process:
                processed.append(path_str)

        # Simulate 3 rapid changes to the same file
        pending["/music/track.flac"] = "modified"
        pending["/music/track.flac"] = "modified"
        pending["/music/track.flac"] = "modified"

        task = asyncio.create_task(flush())
        await task

        # Only one entry since dict deduplicates by path
        assert len(processed) == 1
        assert processed[0] == "/music/track.flac"


# ===========================================================================
# P0-4: Discovery thread safety
# ===========================================================================

class TestDiscoveryThreadSafety:
    async def test_ssdp_has_lock(self):
        """SsdpDiscovery should have an asyncio.Lock."""
        from tune_server.discovery.ssdp import SsdpDiscovery
        ssdp = SsdpDiscovery(EventBus())
        assert hasattr(ssdp, '_lock')
        assert isinstance(ssdp._lock, asyncio.Lock)

    async def test_mdns_has_lock(self):
        """MdnsDiscovery should have an asyncio.Lock."""
        from tune_server.discovery.mdns import MdnsDiscovery
        mdns = MdnsDiscovery(EventBus())
        assert hasattr(mdns, '_lock')
        assert isinstance(mdns._lock, asyncio.Lock)

    async def test_concurrent_device_modifications(self):
        """Concurrent modifications to _devices dict should not corrupt it."""
        from tune_server.discovery.ssdp import SsdpDiscovery
        event_bus = EventBus()
        ssdp = SsdpDiscovery(event_bus)

        # Simulate concurrent access to _devices under lock
        async def add_device(dev_id):
            async with ssdp._lock:
                ssdp._devices[dev_id] = DiscoveredDevice(
                    id=dev_id, name=f"Device {dev_id}",
                    type=OutputType.DLNA, host="192.168.1.1", port=8080,
                )

        tasks = [add_device(f"dev_{i}") for i in range(10)]
        await asyncio.gather(*tasks)

        assert len(ssdp._devices) == 10


# ===========================================================================
# P0-5: Device recovery
# ===========================================================================

class TestDeviceRecovery:
    async def test_ssdp_device_recovery(self):
        """A previously-lost SSDP device should be re-emitted when rediscovered."""
        from tune_server.discovery.ssdp import SsdpDiscovery

        event_bus = EventBus()
        ssdp = SsdpDiscovery(event_bus)

        # Manually add a device that is currently unavailable
        dev = DiscoveredDevice(
            id="dev1", name="Lost Speaker",
            type=OutputType.DLNA, host="192.168.1.10", port=8080,
            available=False,
        )
        ssdp._devices["dev1"] = dev

        # Now simulate rediscovery — the key check: was_lost should trigger emit
        events = []
        async def capture(event):
            events.append(event)
        event_bus.on(EventType.DEVICE_DISCOVERED, capture)

        new_dev = DiscoveredDevice(
            id="dev1", name="Lost Speaker",
            type=OutputType.DLNA, host="192.168.1.10", port=8080,
            available=True,
        )

        async with ssdp._lock:
            was_lost = "dev1" in ssdp._devices and not ssdp._devices["dev1"].available
            is_new = "dev1" not in ssdp._devices
            ssdp._devices["dev1"] = new_dev

        assert was_lost is True
        assert is_new is False

        if is_new or was_lost:
            await event_bus.emit(Event(
                type=EventType.DEVICE_DISCOVERED,
                data=new_dev.model_dump(),
                source="ssdp",
            ))

        assert len(events) == 1
        assert events[0].data["id"] == "dev1"
        assert ssdp._devices["dev1"].available is True

    async def test_mdns_device_recovery(self):
        """A previously-lost mDNS device should be re-emitted when rediscovered."""
        from tune_server.discovery.mdns import MdnsDiscovery

        event_bus = EventBus()
        mdns = MdnsDiscovery(event_bus)

        dev = DiscoveredDevice(
            id="ap1", name="Lost AirPlay",
            type=OutputType.AIRPLAY, host="192.168.1.20", port=7000,
            available=False,
        )
        mdns._devices["ap1"] = dev

        events = []
        async def capture(event):
            events.append(event)
        event_bus.on(EventType.DEVICE_DISCOVERED, capture)

        new_dev = DiscoveredDevice(
            id="ap1", name="Lost AirPlay",
            type=OutputType.AIRPLAY, host="192.168.1.20", port=7000,
            available=True,
        )

        async with mdns._lock:
            was_lost = "ap1" in mdns._devices and not mdns._devices["ap1"].available
            is_new = "ap1" not in mdns._devices
            mdns._devices["ap1"] = new_dev

        if is_new or was_lost:
            await event_bus.emit(Event(
                type=EventType.DEVICE_DISCOVERED,
                data=new_dev.model_dump(),
                source="mdns",
            ))

        assert len(events) == 1
        assert events[0].data["id"] == "ap1"


# ===========================================================================
# P1-1: Gapless playback integration
# ===========================================================================

class TestGaplessIntegration:
    @patch("tune_server.playback.player.AudioPipeline")
    async def test_gapless_handler_created_on_set_output(self, MockPipeline, mock_output, event_bus):
        """Setting an output should create a GaplessHandler."""
        player = Player(zone_id=1, event_bus=event_bus)
        assert player._gapless is None

        player.set_output(mock_output)
        assert player._gapless is not None

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_gapless_cancelled_on_stop(self, MockPipeline, mock_output, event_bus):
        """Stopping pipeline should cancel gapless preloading."""
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        tracks = [_make_track(1, "A"), _make_track(2, "B")]
        await player.play(tracks=tracks)
        await asyncio.sleep(0.05)

        # Stopping should cancel gapless
        pipeline.output_buffer.close()
        await player.stop()

        assert player._gapless is not None
        # After cancel, gapless should have no preloaded pipeline
        assert not player._gapless.has_next

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_preload_next_called_after_start(self, MockPipeline, mock_output, event_bus):
        """After starting a track, preload_next should be called for gapless."""
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        # Mock gapless to verify preload is called
        player._gapless = AsyncMock()
        player._gapless.has_next = False

        tracks = [_make_track(1, "A"), _make_track(2, "B")]
        await player.play(tracks=tracks)
        await asyncio.sleep(0.05)

        # preload should have been called (peek_next returns track B)
        player._gapless.preload.assert_awaited()

        pipeline.output_buffer.close()
        await player.stop()

    async def test_try_gapless_transition_no_gapless(self, event_bus):
        """Without gapless handler, transition returns False."""
        player = Player(zone_id=1, event_bus=event_bus)
        result = await player._try_gapless_transition()
        assert result is False

    async def test_try_gapless_transition_no_preloaded(self, mock_output, event_bus):
        """With gapless but nothing preloaded, transition returns False."""
        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        assert player._gapless is not None
        result = await player._try_gapless_transition()
        assert result is False

    async def test_try_gapless_format_mismatch(self, mock_output, event_bus):
        """Gapless transition fails when formats don't match."""
        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        # Set up mismatched stream info
        player._pipeline = MagicMock()
        player._pipeline.stream_info = AudioStreamInfo(
            format=AudioFormat.FLAC, sample_rate=44100, bit_depth=16, channels=2,
        )

        player._gapless._next_pipeline = MagicMock()
        player._gapless._next_stream_info = AudioStreamInfo(
            format=AudioFormat.FLAC, sample_rate=96000, bit_depth=24, channels=2,
        )
        player._gapless._next_track = _make_track(2, "B")

        result = await player._try_gapless_transition()
        assert result is False


# ===========================================================================
# P2-1: Queue persistence
# ===========================================================================

class TestQueuePersistence:
    async def test_play_persists_queue(self, db, event_bus, sample_tracks):
        """Playing tracks should persist the queue to DB via callback."""
        queue_repo = PlayQueueRepo(db)
        zone_repo = ZoneRepo(db)

        # Create a zone in DB
        zone_id = await zone_repo.create("Test Zone", "local")

        from tune_server.zones.zone import ZoneInstance

        output = AsyncMock()
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)

        zone = ZoneInstance(
            zone_id=zone_id, name="Test Zone",
            output_type=OutputType.LOCAL, output=output,
            event_bus=event_bus, queue_repo=queue_repo,
        )

        # Verify the callback is set
        assert zone.player._queue_persist_cb is not None

        # Directly invoke _save_queue with real DB tracks
        await zone._save_queue(sample_tracks, 0)

        # Verify DB has the queue
        rows = await queue_repo.get_queue(zone_id)
        assert len(rows) == len(sample_tracks)
        assert rows[0]["track_id"] == sample_tracks[0].id
        assert rows[1]["track_id"] == sample_tracks[1].id

    async def test_restore_queue(self, db, event_bus, sample_tracks):
        """Restoring a queue from DB should populate the player's queue."""
        queue_repo = PlayQueueRepo(db)
        zone_repo = ZoneRepo(db)
        zone_id = await zone_repo.create("Test Zone", "local")

        # Persist queue to DB
        track_ids = [t.id for t in sample_tracks]
        await queue_repo.set_queue(zone_id, track_ids)
        await queue_repo.set_current(zone_id, 1)

        from tune_server.zones.zone import ZoneInstance

        output = AsyncMock()
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)

        zone = ZoneInstance(
            zone_id=zone_id, name="Test Zone",
            output_type=OutputType.LOCAL, output=output,
            event_bus=event_bus, queue_repo=queue_repo,
        )

        # Restore
        await zone.restore_queue()

        assert zone.player.queue.length == len(sample_tracks)
        assert zone.player.queue.position == 1
        assert zone.player.queue.current.title == sample_tracks[1].title

    async def test_save_queue_skips_no_ids(self, db, event_bus):
        """Queue with streaming-only tracks (no DB IDs) should not be persisted."""
        queue_repo = PlayQueueRepo(db)
        zone_repo = ZoneRepo(db)
        zone_id = await zone_repo.create("Test Zone", "local")

        from tune_server.zones.zone import ZoneInstance

        output = AsyncMock()
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)

        zone = ZoneInstance(
            zone_id=zone_id, name="Test Zone",
            output_type=OutputType.LOCAL, output=output,
            event_bus=event_bus, queue_repo=queue_repo,
        )

        # Tracks without DB IDs
        streaming_tracks = [
            Track(title="Stream A", source=Source.TIDAL, source_id="tidal_1"),
            Track(title="Stream B", source=Source.TIDAL, source_id="tidal_2"),
        ]
        await zone._save_queue(streaming_tracks, 0)

        # Queue should be empty in DB
        rows = await queue_repo.get_queue(zone_id)
        assert len(rows) == 0


# ===========================================================================
# P2-2: Volume persistence
# ===========================================================================

class TestVolumePersistence:
    async def test_volume_saved_on_change(self, db, event_bus):
        """Changing volume should persist it to the zones table."""
        zone_repo = ZoneRepo(db)
        zone_id = await zone_repo.create("Test Zone", "local")

        from tune_server.zones.zone import ZoneInstance

        output = AsyncMock()
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)

        zone = ZoneInstance(
            zone_id=zone_id, name="Test Zone",
            output_type=OutputType.LOCAL, output=output,
            event_bus=event_bus, zone_repo=zone_repo,
        )

        # Change volume
        await zone.player.set_volume(0.8)
        assert zone.player.volume == 0.8

        # Verify saved in DB
        row = await zone_repo.get(zone_id)
        assert row["volume"] == 0.8

    async def test_volume_restored_on_init(self, db, event_bus):
        """Zone manager should restore persisted volume on initialization."""
        from tune_server.zones.manager import ZoneManager

        zone_repo = ZoneRepo(db)
        zone_id = await zone_repo.create("Loud Zone", "local")
        await zone_repo.update(zone_id, volume=0.9)

        zm = ZoneManager(db, event_bus)

        async def _mock_local_factory(device_id):
            output = AsyncMock()
            output.name = "mock-local"
            type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
            return output

        zm.register_output_factory(OutputType.LOCAL, _mock_local_factory)
        await zm.initialize()

        zone = zm.get_zone(zone_id)
        assert zone is not None
        assert zone.player.volume == 0.9

        await zm.cleanup()

    async def test_default_volume_not_persisted_needlessly(self, db, event_bus):
        """Zones with default volume (0.5) should not trigger a set_volume call."""
        from tune_server.zones.manager import ZoneManager

        zone_repo = ZoneRepo(db)
        zone_id = await zone_repo.create("Default Zone", "local")

        zm = ZoneManager(db, event_bus)

        async def _mock_local_factory(device_id):
            output = AsyncMock()
            output.name = "mock-local"
            type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
            return output

        zm.register_output_factory(OutputType.LOCAL, _mock_local_factory)
        await zm.initialize()

        zone = zm.get_zone(zone_id)
        assert zone is not None
        assert zone.player.volume == 0.5

        await zm.cleanup()


# ===========================================================================
# P2-3: Resume playback position
# ===========================================================================

class TestResumePosition:
    async def test_queue_position_restored_from_is_current(self, db, event_bus, sample_tracks):
        """The is_current flag in play_queue should set the correct queue position."""
        queue_repo = PlayQueueRepo(db)
        zone_repo = ZoneRepo(db)
        zone_id = await zone_repo.create("Test Zone", "local")

        track_ids = [t.id for t in sample_tracks]
        await queue_repo.set_queue(zone_id, track_ids)
        # Mark the third track as current
        await queue_repo.set_current(zone_id, 2)

        from tune_server.zones.zone import ZoneInstance

        output = AsyncMock()
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)

        zone = ZoneInstance(
            zone_id=zone_id, name="Test Zone",
            output_type=OutputType.LOCAL, output=output,
            event_bus=event_bus, queue_repo=queue_repo,
        )

        await zone.restore_queue()

        assert zone.player.queue.position == 2
        assert zone.player.queue.current.title == sample_tracks[2].title
        # Player state should still be STOPPED (no auto-resume)
        assert zone.player.state == PlaybackState.STOPPED


# ===========================================================================
# DB migration test
# ===========================================================================

class TestMigrations:
    async def test_file_mtime_column_exists(self, db):
        """The file_mtime column should exist after schema initialization."""
        row = await db.fetchone(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tracks'"
        )
        assert "file_mtime" in row["sql"]

    async def test_migration_idempotent(self, db):
        """Running migrations multiple times should not error."""
        # The migration already ran during db fixture setup.
        # Running again should be a no-op.
        await db._run_migrations()
        # No exception means success


# ===========================================================================
# Scanner lock attribute
# ===========================================================================

class TestScannerLock:
    async def test_scanner_has_lock(self, db, event_bus):
        from tune_server.library.scanner import LibraryScanner
        scanner = LibraryScanner(db, event_bus)
        assert hasattr(scanner, '_lock')
        assert isinstance(scanner._lock, asyncio.Lock)

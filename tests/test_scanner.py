from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tune_server.event_bus import EventBus, EventType
from tune_server.library.scanner import LibraryScanner


@pytest.fixture
def scanner(db, event_bus):
    return LibraryScanner(db, event_bus)


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


async def test_is_scanning_default_false(scanner):
    assert scanner.is_scanning is False


@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_empty_dir(mock_read, mock_art, scanner, tmp_path):
    stats = await scanner.scan([str(tmp_path / "nonexistent")])
    assert stats["added"] == 0
    assert stats["scanned"] == 0
    assert stats["removed"] == 0


@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_adds_tracks(mock_read, mock_art, scanner, tmp_path):
    mock_read.return_value = _make_metadata()

    # Create mock FLAC files
    (tmp_path / "track1.flac").touch()
    (tmp_path / "track2.flac").touch()

    stats = await scanner.scan([str(tmp_path)])
    assert stats["added"] == 2
    assert stats["scanned"] == 2


@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_skips_existing(mock_read, mock_art, scanner, tmp_path):
    mock_read.return_value = _make_metadata()

    (tmp_path / "track1.flac").touch()

    # First scan adds
    stats1 = await scanner.scan([str(tmp_path)])
    assert stats1["added"] == 1

    # Second scan skips
    stats2 = await scanner.scan([str(tmp_path)])
    assert stats2["added"] == 0
    assert stats2["scanned"] == 1


@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_removes_deleted(mock_read, mock_art, scanner, tmp_path):
    mock_read.return_value = _make_metadata()

    track_file = tmp_path / "track1.flac"
    track_file.touch()

    await scanner.scan([str(tmp_path)])

    # Delete the file
    track_file.unlink()

    stats = await scanner.scan([str(tmp_path)])
    assert stats["removed"] == 1


@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_emits_events(mock_read, mock_art, scanner, event_bus, tmp_path):
    mock_read.return_value = _make_metadata()
    (tmp_path / "track.flac").touch()

    events = []
    event_bus.on(EventType.LIBRARY_SCAN_STARTED, lambda e: events.append(e.type))
    event_bus.on(EventType.LIBRARY_SCAN_COMPLETED, lambda e: events.append(e.type))

    await scanner.scan([str(tmp_path)])

    assert EventType.LIBRARY_SCAN_STARTED in events
    assert EventType.LIBRARY_SCAN_COMPLETED in events


@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_already_in_progress(mock_read, mock_art, scanner, tmp_path):
    mock_read.return_value = _make_metadata()
    (tmp_path / "track.flac").touch()

    # Manually set scanning flag
    scanner._scanning = True
    result = await scanner.scan([str(tmp_path)])
    assert result["status"] == "already_scanning"
    scanner._scanning = False


@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_process_file_creates_artist_album(mock_read, mock_art, scanner, tmp_path):
    mock_read.return_value = _make_metadata(
        title="Test Track",
        artist="Test Artist",
        album="Test Album",
    )

    (tmp_path / "track.flac").touch()
    stats = await scanner.scan([str(tmp_path)])
    assert stats["added"] == 1

    # Verify artist and album were created
    artists = await scanner._artist_repo.search("Test Artist")
    assert len(artists) >= 1


@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_single_updates(mock_read, mock_art, scanner, tmp_path):
    mock_read.return_value = _make_metadata(title="Original")

    track_file = tmp_path / "track.flac"
    track_file.touch()

    # First add via scan
    await scanner.scan([str(tmp_path)])

    # Then update via scan_single
    mock_read.return_value = _make_metadata(title="Updated")
    result = await scanner.scan_single(str(track_file))
    assert result is True


@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_metadata_error(mock_read, mock_art, scanner, tmp_path):
    mock_read.return_value = None  # Simulate metadata read failure

    (tmp_path / "track.flac").touch()
    stats = await scanner.scan([str(tmp_path)])
    assert stats["added"] == 0
    assert stats["scanned"] == 1

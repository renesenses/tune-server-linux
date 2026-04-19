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


# ---------------------------------------------------------------------------
# Unit tests for _quality_suffix and _same_quality_tier
# ---------------------------------------------------------------------------


class TestQualitySuffix:
    def test_standard_cd_quality(self):
        from tune_server.library.scanner import _quality_suffix

        assert _quality_suffix(44100, 16) == "44.1kHz"

    def test_hires_96_24(self):
        from tune_server.library.scanner import _quality_suffix

        assert _quality_suffix(96000, 24) == "96kHz/24bit"

    def test_hires_192_24(self):
        from tune_server.library.scanner import _quality_suffix

        assert _quality_suffix(192000, 24) == "192kHz/24bit"

    def test_48khz_16bit(self):
        from tune_server.library.scanner import _quality_suffix

        assert _quality_suffix(48000, 16) == "48kHz"

    def test_no_sample_rate(self):
        from tune_server.library.scanner import _quality_suffix

        assert _quality_suffix(None, 24) == ""

    def test_zero_sample_rate(self):
        from tune_server.library.scanner import _quality_suffix

        assert _quality_suffix(0, 16) == ""

    def test_bit_depth_none(self):
        from tune_server.library.scanner import _quality_suffix

        # bit_depth=None should not append bit depth info
        assert _quality_suffix(96000, None) == "96kHz"


class TestSameQualityTier:
    def test_same_rate(self):
        from tune_server.library.scanner import _same_quality_tier

        assert _same_quality_tier(44100, 44100) is True

    def test_different_rate(self):
        from tune_server.library.scanner import _same_quality_tier

        assert _same_quality_tier(44100, 96000) is False

    def test_first_none(self):
        from tune_server.library.scanner import _same_quality_tier

        assert _same_quality_tier(None, 96000) is True

    def test_second_none(self):
        from tune_server.library.scanner import _same_quality_tier

        assert _same_quality_tier(44100, None) is True

    def test_both_none(self):
        from tune_server.library.scanner import _same_quality_tier

        assert _same_quality_tier(None, None) is True


# ---------------------------------------------------------------------------
# Artist name population on albums (both get_or_create paths)
# ---------------------------------------------------------------------------


@patch("tune_server.library.scanner.fetch_cover_from_musicbrainz", return_value=None)
@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_artist_name_set_on_album_from_metadata(
    mock_read, mock_art, mock_mb, scanner, tmp_path
):
    """When metadata provides artist, the created album should reference the correct artist."""
    mock_read.return_value = _make_metadata(
        title="Song One",
        artist="Miles Davis",
        album="Kind of Blue",
    )
    (tmp_path / "song.flac").touch()

    await scanner.scan([str(tmp_path)])

    artists = await scanner._artist_repo.search("Miles Davis")
    assert len(artists) >= 1
    artist = artists[0]

    albums = await scanner._album_repo.list()
    assert len(albums) >= 1
    album = [a for a in albums if a.title == "Kind of Blue"][0]
    assert album.artist_id == artist.id


@patch("tune_server.library.scanner.fetch_cover_from_musicbrainz", return_value=None)
@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_artist_name_set_on_album_with_album_artist(
    mock_read, mock_art, mock_mb, scanner, tmp_path
):
    """When album_artist is set, it should be preferred over track artist for album lookup."""
    from tune_server.library.metadata_reader import TrackMetadata

    mock_read.return_value = TrackMetadata(
        title="So What",
        artist="Miles Davis",
        album="Kind of Blue",
        album_artist="Miles Davis Quintet",
        track_number=1,
        disc_number=1,
        year=1959,
        genre="Jazz",
        duration_ms=540000,
        format="flac",
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        has_cover=False,
    )
    (tmp_path / "so_what.flac").touch()

    await scanner.scan([str(tmp_path)])

    # album_artist should be used, not track artist
    artists = await scanner._artist_repo.search("Miles Davis Quintet")
    assert len(artists) >= 1
    artist = artists[0]

    albums = await scanner._album_repo.list()
    album = [a for a in albums if a.title == "Kind of Blue"][0]
    assert album.artist_id == artist.id


@patch("tune_server.library.scanner.fetch_cover_from_musicbrainz", return_value=None)
@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_album_get_or_create_reuses_existing(
    mock_read, mock_art, mock_mb, scanner, tmp_path
):
    """Two tracks from the same album/artist should share the same album entry."""
    mock_read.return_value = _make_metadata(
        title="Track 1", artist="TestArtist", album="TestAlbum"
    )
    (tmp_path / "track1.flac").touch()
    (tmp_path / "track2.flac").touch()

    await scanner.scan([str(tmp_path)])

    albums = await scanner._album_repo.list()
    matching = [a for a in albums if a.title == "TestAlbum"]
    assert len(matching) == 1, "Two tracks for the same album should share one album row"


# ---------------------------------------------------------------------------
# Quality-based album splitting
# ---------------------------------------------------------------------------


@patch("tune_server.library.scanner.fetch_cover_from_musicbrainz", return_value=None)
@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_quality_based_album_split(
    mock_read, mock_art, mock_mb, scanner, tmp_path
):
    """Tracks with different sample rates should be split into separate albums."""
    from tune_server.library.metadata_reader import TrackMetadata

    # First track: CD quality
    cd_meta = TrackMetadata(
        title="Track CD",
        artist="SplitArtist",
        album="SplitAlbum",
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
    # Second track: Hi-Res quality
    hires_meta = TrackMetadata(
        title="Track HiRes",
        artist="SplitArtist",
        album="SplitAlbum",
        album_artist=None,
        track_number=1,
        disc_number=1,
        year=2024,
        genre="Rock",
        duration_ms=240000,
        format="flac",
        sample_rate=96000,
        bit_depth=24,
        channels=2,
        has_cover=False,
    )

    call_count = 0

    def side_effect(path):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return cd_meta
        return hires_meta

    mock_read.side_effect = side_effect

    (tmp_path / "track_cd.flac").touch()
    (tmp_path / "track_hires.flac").touch()

    await scanner.scan([str(tmp_path)])

    albums = await scanner._album_repo.list()
    titles = sorted([a.title for a in albums])
    assert "SplitAlbum" in titles
    assert "SplitAlbum (96kHz/24bit)" in titles


# ---------------------------------------------------------------------------
# Duplicate file handling (same audio_hash)
# ---------------------------------------------------------------------------


@patch("tune_server.library.scanner.fetch_cover_from_musicbrainz", return_value=None)
@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.compute_audio_hash", return_value="deadbeef1234")
@patch("tune_server.library.scanner.read_metadata")
async def test_duplicate_audio_hash_skipped(
    mock_read, mock_hash, mock_art, mock_mb, scanner, tmp_path
):
    """A file with the same audio_hash as an existing track should not be added."""
    mock_read.return_value = _make_metadata(title="Dupe Track", artist="DupeArtist", album="DupeAlbum")

    (tmp_path / "original.flac").touch()
    (tmp_path / "copy.flac").touch()

    stats = await scanner.scan([str(tmp_path)])
    # First file should be added, second should be skipped as duplicate
    assert stats["added"] == 1
    assert stats["scanned"] == 2


# ---------------------------------------------------------------------------
# Scanner with empty directory (existing dir, no audio files)
# ---------------------------------------------------------------------------


@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_empty_existing_dir(mock_read, mock_art, scanner, tmp_path):
    """Scanning an existing but empty directory should produce zero results."""
    empty_dir = tmp_path / "empty_music"
    empty_dir.mkdir()

    stats = await scanner.scan([str(empty_dir)])
    assert stats["scanned"] == 0
    assert stats["added"] == 0
    assert stats["errors"] == 0
    mock_read.assert_not_called()


@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_dir_with_non_audio_files(mock_read, mock_art, scanner, tmp_path):
    """Non-audio files (txt, jpg, etc.) should be ignored by the scanner."""
    (tmp_path / "readme.txt").touch()
    (tmp_path / "cover.jpg").touch()
    (tmp_path / "notes.pdf").touch()

    stats = await scanner.scan([str(tmp_path)])
    assert stats["scanned"] == 0
    assert stats["added"] == 0
    mock_read.assert_not_called()


# ---------------------------------------------------------------------------
# Scanner with nested subdirectories
# ---------------------------------------------------------------------------


@patch("tune_server.library.scanner.fetch_cover_from_musicbrainz", return_value=None)
@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_nested_subdirectories(
    mock_read, mock_art, mock_mb, scanner, tmp_path
):
    """Scanner should recurse into nested subdirectories to find audio files."""
    mock_read.return_value = _make_metadata()

    # Create nested structure: Artist/Album/track.flac
    nested = tmp_path / "Artist" / "Album"
    nested.mkdir(parents=True)
    (nested / "01_track.flac").touch()

    deeper = tmp_path / "Artist" / "Album2" / "Disc1"
    deeper.mkdir(parents=True)
    (deeper / "01_deep.flac").touch()

    stats = await scanner.scan([str(tmp_path)])
    assert stats["scanned"] == 2
    assert stats["added"] == 2


@patch("tune_server.library.scanner.fetch_cover_from_musicbrainz", return_value=None)
@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_skips_excluded_dirs(
    mock_read, mock_art, mock_mb, scanner, tmp_path
):
    """Directories in SKIP_DIRS (e.g. 'duplicates', '.tune') should be skipped."""
    mock_read.return_value = _make_metadata()

    # Normal track
    (tmp_path / "good.flac").touch()

    # Tracks inside excluded directories
    skip_dir = tmp_path / "duplicates"
    skip_dir.mkdir()
    (skip_dir / "dupe.flac").touch()

    tune_dir = tmp_path / ".tune"
    tune_dir.mkdir()
    (tune_dir / "hidden.flac").touch()

    stats = await scanner.scan([str(tmp_path)])
    assert stats["scanned"] == 1
    assert stats["added"] == 1


# ---------------------------------------------------------------------------
# Metadata extraction failures (corrupt/unreadable files)
# ---------------------------------------------------------------------------


@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_corrupt_file_returns_none(mock_read, mock_art, scanner, tmp_path):
    """Corrupt file (read_metadata returns None) should be counted as scanned but not added."""
    mock_read.return_value = None

    (tmp_path / "corrupt.flac").touch()
    (tmp_path / "also_corrupt.mp3").touch()

    stats = await scanner.scan([str(tmp_path)])
    assert stats["scanned"] == 2
    assert stats["added"] == 0
    assert stats["errors"] == 0


@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_metadata_exception_counted_as_error(
    mock_read, mock_art, scanner, tmp_path
):
    """If _process_new_file raises an exception, it should be counted as an error."""
    mock_read.side_effect = Exception("Unexpected mutagen crash")

    (tmp_path / "crash.flac").touch()

    stats = await scanner.scan([str(tmp_path)])
    assert stats["scanned"] == 1
    assert stats["added"] == 0
    assert stats["errors"] == 1


@patch("tune_server.library.scanner.fetch_cover_from_musicbrainz", return_value=None)
@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.compute_audio_hash", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_scan_audio_hash_failure_skips_track(
    mock_read, mock_hash, mock_art, mock_mb, scanner, tmp_path
):
    """If compute_audio_hash returns None, the track should not be added."""
    mock_read.return_value = _make_metadata()

    (tmp_path / "unreadable.flac").touch()

    stats = await scanner.scan([str(tmp_path)])
    assert stats["scanned"] == 1
    assert stats["added"] == 0


# ---------------------------------------------------------------------------
# Artist fallback from file path
# ---------------------------------------------------------------------------


@patch("tune_server.library.scanner.fetch_cover_from_musicbrainz", return_value=None)
@patch("tune_server.library.scanner.get_album_artwork", return_value=None)
@patch("tune_server.library.scanner.read_metadata")
async def test_artist_fallback_from_path(
    mock_read, mock_art, mock_mb, scanner, tmp_path
):
    """When metadata has no artist, scanner should extract artist from path."""
    from tune_server.library.metadata_reader import TrackMetadata

    mock_read.return_value = TrackMetadata(
        title="Instrumental",
        artist="",
        album="",
        album_artist=None,
        track_number=1,
        disc_number=1,
        year=None,
        genre=None,
        duration_ms=180000,
        format="flac",
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        has_cover=False,
    )

    # Path: .../PathArtist/PathAlbum/track.flac
    artist_dir = tmp_path / "PathArtist" / "PathAlbum"
    artist_dir.mkdir(parents=True)
    (artist_dir / "track.flac").touch()

    await scanner.scan([str(tmp_path)])

    artists = await scanner._artist_repo.search("PathArtist")
    assert len(artists) >= 1, "Artist should be extracted from directory path"


# ---------------------------------------------------------------------------
# _list_audio_files unit test
# ---------------------------------------------------------------------------


def test_list_audio_files_dot_underscore_skipped(tmp_path):
    """Files starting with ._ (macOS resource forks) should be excluded."""
    from tune_server.library.scanner import _list_audio_files

    (tmp_path / "good.flac").touch()
    (tmp_path / "._good.flac").touch()

    result = _list_audio_files(tmp_path)
    assert len(result) == 1
    assert result[0].name == "good.flac"

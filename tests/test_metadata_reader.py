from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from tune_server.library.metadata_reader import (
    SUPPORTED_EXTENSIONS,
    _detect_format,
    _get_first,
    _parse_int,
    read_metadata,
)


# --- Pure utility functions ---


def test_parse_int_simple():
    assert _parse_int("3") == 3


def test_parse_int_fraction():
    assert _parse_int("3/12") == 3


def test_parse_int_invalid():
    assert _parse_int("abc") == 0
    assert _parse_int("abc", default=5) == 5


def test_detect_format_flac():
    assert _detect_format(Path("x.flac")) == "flac"


def test_detect_format_m4a():
    assert _detect_format(Path("x.m4a")) == "aac"


def test_detect_format_unknown():
    assert _detect_format(Path("x.xyz")) == "xyz"


def test_get_first_found():
    tags = {"artist": "Bashung", "title": "La nuit je mens"}
    assert _get_first(tags, ["artist"]) == "Bashung"


def test_get_first_list_value():
    tags = {"artist": ["Bashung", "Another"]}
    assert _get_first(tags, ["artist"]) == "Bashung"


def test_get_first_default():
    tags = {"title": "x"}
    assert _get_first(tags, ["artist", "composer"], default="Unknown") == "Unknown"


def test_supported_extensions():
    expected = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wv", ".wma", ".dsf", ".dff", ".alac"}
    assert expected == SUPPORTED_EXTENSIONS


# --- read_metadata with mocked mutagen ---


@patch("tune_server.library.metadata_reader.MutagenFile")
def test_read_metadata_flac(mock_mutagen_file):
    from mutagen.flac import FLAC

    audio = MagicMock(spec=FLAC)
    audio.tags = {
        "title": ["La nuit je mens"],
        "artist": ["Alain Bashung"],
        "album": ["Fantaisie Militaire"],
        "tracknumber": ["3"],
        "discnumber": ["1"],
        "date": ["1998"],
        "genre": ["Chanson"],
    }
    audio.info = MagicMock()
    audio.info.sample_rate = 44100
    audio.info.bits_per_sample = 16
    audio.info.length = 240.5
    audio.info.channels = 2
    audio.pictures = [MagicMock()]
    mock_mutagen_file.return_value = audio

    result = read_metadata("/music/test.flac")
    assert result is not None
    assert result.title == "La nuit je mens"
    assert result.artist == "Alain Bashung"
    assert result.album == "Fantaisie Militaire"
    assert result.track_number == 3
    assert result.format == "flac"
    assert result.has_cover is True


@patch("tune_server.library.metadata_reader.MutagenFile")
def test_read_metadata_mp3(mock_mutagen_file):
    from mutagen.mp3 import MP3

    audio = MagicMock(spec=MP3)
    # Use a MagicMock for tags so we can control keys() behavior
    tags = MagicMock()
    tag_data = {
        "TIT2": MagicMock(__str__=lambda self: "Title"),
        "TPE1": MagicMock(__str__=lambda self: "Artist"),
        "TALB": MagicMock(__str__=lambda self: "Album"),
        "TRCK": MagicMock(__str__=lambda self: "5/12"),
    }
    tags.get = lambda k, d=None: tag_data.get(k, d)
    tags.__contains__ = lambda self, k: k in tag_data
    tags.__getitem__ = lambda self, k: tag_data[k]
    tags.keys.return_value = list(tag_data.keys())
    tags.__iter__ = lambda self: iter(tag_data)
    audio.tags = tags
    audio.info = MagicMock()
    audio.info.sample_rate = 44100
    audio.info.length = 200.0
    audio.info.channels = 2
    mock_mutagen_file.return_value = audio

    result = read_metadata("/music/test.mp3")
    assert result is not None
    assert result.format == "mp3"


def test_read_metadata_unsupported_extension():
    result = read_metadata("/music/readme.txt")
    assert result is None


@patch("tune_server.library.metadata_reader.MutagenFile")
def test_read_metadata_mutagen_returns_none(mock_mutagen_file):
    mock_mutagen_file.return_value = None
    result = read_metadata("/music/test.flac")
    assert result is None


@patch("tune_server.library.metadata_reader.MutagenFile")
def test_read_metadata_exception(mock_mutagen_file):
    mock_mutagen_file.side_effect = Exception("read error")
    result = read_metadata("/music/test.flac")
    assert result is None

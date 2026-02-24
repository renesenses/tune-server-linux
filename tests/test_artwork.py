from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from tune_server.library.artwork import _hash_path, extract_cover_art, get_album_artwork, save_artwork


def test_hash_path():
    result = _hash_path("/music/test.flac")
    expected = hashlib.md5("/music/test.flac".encode()).hexdigest()
    assert result == expected
    # Deterministic
    assert _hash_path("/music/test.flac") == _hash_path("/music/test.flac")


@patch("tune_server.library.artwork.MutagenFile")
def test_extract_cover_art_flac(mock_mutagen):
    from mutagen.flac import FLAC

    audio = MagicMock(spec=FLAC)
    picture = MagicMock()
    picture.data = b"\x89PNG_image_data"
    audio.pictures = [picture]
    mock_mutagen.return_value = audio

    result = extract_cover_art("/music/test.flac")
    assert result == b"\x89PNG_image_data"


@patch("tune_server.library.artwork.MutagenFile")
def test_extract_cover_art_none(mock_mutagen):
    from mutagen.flac import FLAC

    audio = MagicMock(spec=FLAC)
    audio.pictures = []
    mock_mutagen.return_value = audio

    # Also ensure no folder images exist
    with patch.object(Path, "parent", new_callable=lambda: property(lambda self: Path("/nonexistent"))):
        result = extract_cover_art("/nonexistent/test.flac")
    # Falls through to folder check, but folder doesn't exist
    assert result is None


@patch("tune_server.library.artwork.MutagenFile")
def test_extract_cover_art_folder_fallback(mock_mutagen, tmp_path):
    from mutagen.flac import FLAC

    audio = MagicMock(spec=FLAC)
    audio.pictures = []
    mock_mutagen.return_value = audio

    # Create a folder.jpg
    folder_jpg = tmp_path / "folder.jpg"
    folder_jpg.write_bytes(b"\xff\xd8\xff_jpeg_data")

    track_path = tmp_path / "test.flac"
    result = extract_cover_art(str(track_path))
    assert result == b"\xff\xd8\xff_jpeg_data"


@patch("tune_server.library.artwork.Image")
@patch("tune_server.library.artwork._get_cache_dir")
def test_save_artwork(mock_cache_dir, mock_image_module, tmp_path):
    mock_cache_dir.return_value = tmp_path

    mock_img = MagicMock()
    mock_img.width = 500
    mock_img.height = 500
    mock_image_module.open.return_value = mock_img
    mock_img.convert.return_value = mock_img

    result = save_artwork("/music/test.flac", b"image_data")
    assert result is not None
    mock_img.convert.assert_called_once_with("RGB")
    mock_img.save.assert_called_once()


@patch("tune_server.library.artwork._get_cache_dir")
def test_get_album_artwork_cached(mock_cache_dir, tmp_path):
    mock_cache_dir.return_value = tmp_path

    # Pre-create cached file
    hash_name = _hash_path("/music/test.flac")
    cached_file = tmp_path / f"{hash_name}.jpg"
    cached_file.write_bytes(b"cached_image")

    result = get_album_artwork("/music/test.flac")
    assert result == str(cached_file)

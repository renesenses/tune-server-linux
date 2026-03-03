from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog
from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.wavpack import WavPack

logger = structlog.get_logger()

SUPPORTED_EXTENSIONS = {
    ".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aiff",
    ".aif", ".wv", ".wma", ".dsf", ".dff", ".alac",
}


@dataclass
class TrackMetadata:
    title: str
    artist: str
    album: str
    album_artist: Optional[str]
    track_number: int
    disc_number: int
    year: Optional[int]
    genre: Optional[str]
    duration_ms: int
    format: str
    sample_rate: int
    bit_depth: int
    channels: int
    has_cover: bool


def _get_first(tags: dict, keys: list[str], default: str = "") -> str:
    for key in keys:
        val = tags.get(key)
        if val:
            if isinstance(val, list):
                return str(val[0])
            return str(val)
    return default


def _parse_int(value: str, default: int = 0) -> int:
    try:
        # Handle "3/12" style track numbers
        return int(str(value).split("/")[0])
    except (ValueError, TypeError, IndexError):
        return default


def _detect_format(path: Path) -> str:
    ext = path.suffix.lower()
    format_map = {
        ".flac": "flac",
        ".mp3": "mp3",
        ".m4a": "aac",
        ".ogg": "ogg",
        ".opus": "opus",
        ".wav": "wav",
        ".aiff": "aiff",
        ".aif": "aiff",
        ".wv": "wav",
        ".wma": "wma",
        ".dsf": "dsd",
        ".dff": "dsd",
    }
    return format_map.get(ext, ext.lstrip("."))


def read_metadata(file_path: str) -> Optional[TrackMetadata]:
    path = Path(file_path)

    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None

    try:
        audio = MutagenFile(file_path)
        if audio is None:
            logger.warning("mutagen_unsupported", path=file_path)
            return None

        tags = audio.tags or {}
        info = audio.info

        # Extract metadata based on file type
        if isinstance(audio, FLAC):
            title = _get_first(tags, ["title"], path.stem)
            artist = _get_first(tags, ["artist"], "Unknown Artist")
            album = _get_first(tags, ["album"], "Unknown Album")
            album_artist = _get_first(tags, ["albumartist", "album_artist"]) or None
            track_num = _parse_int(_get_first(tags, ["tracknumber"]))
            disc_num = _parse_int(_get_first(tags, ["discnumber"]), 1)
            year_str = _get_first(tags, ["date", "year"])
            genre = _get_first(tags, ["genre"]) or None
            sample_rate = info.sample_rate
            bit_depth = info.bits_per_sample or 16
            has_cover = len(audio.pictures) > 0

        elif isinstance(audio, MP3):
            title = _get_first(tags, ["TIT2"], path.stem)
            artist = _get_first(tags, ["TPE1"], "Unknown Artist")
            album = _get_first(tags, ["TALB"], "Unknown Album")
            album_artist = _get_first(tags, ["TPE2"]) or None
            track_num = _parse_int(_get_first(tags, ["TRCK"]))
            disc_num = _parse_int(_get_first(tags, ["TPOS"]), 1)
            year_str = _get_first(tags, ["TDRC", "TYER"])
            genre = _get_first(tags, ["TCON"]) or None
            sample_rate = info.sample_rate
            bit_depth = 16
            has_cover = any(k.startswith("APIC") for k in tags.keys()) if tags else False

        elif isinstance(audio, MP4):
            title = _get_first(tags, ["\xa9nam"], path.stem)
            artist = _get_first(tags, ["\xa9ART"], "Unknown Artist")
            album = _get_first(tags, ["\xa9alb"], "Unknown Album")
            album_artist = _get_first(tags, ["aART"]) or None
            trkn = tags.get("trkn", [(0, 0)])[0]
            track_num = trkn[0] if isinstance(trkn, tuple) else _parse_int(str(trkn))
            disk = tags.get("disk", [(1, 1)])[0]
            disc_num = disk[0] if isinstance(disk, tuple) else 1
            year_str = _get_first(tags, ["\xa9day"])
            genre = _get_first(tags, ["\xa9gen"]) or None
            sample_rate = info.sample_rate
            bit_depth = info.bits_per_sample if hasattr(info, "bits_per_sample") else 16
            has_cover = "covr" in tags

        elif isinstance(audio, OggVorbis):
            title = _get_first(tags, ["title"], path.stem)
            artist = _get_first(tags, ["artist"], "Unknown Artist")
            album = _get_first(tags, ["album"], "Unknown Album")
            album_artist = _get_first(tags, ["albumartist"]) or None
            track_num = _parse_int(_get_first(tags, ["tracknumber"]))
            disc_num = _parse_int(_get_first(tags, ["discnumber"]), 1)
            year_str = _get_first(tags, ["date"])
            genre = _get_first(tags, ["genre"]) or None
            sample_rate = info.sample_rate
            bit_depth = 16
            has_cover = False

        else:
            # Generic fallback
            title = _get_first(tags, ["title", "TIT2", "\xa9nam"], path.stem)
            artist = _get_first(tags, ["artist", "TPE1", "\xa9ART"], "Unknown Artist")
            album = _get_first(tags, ["album", "TALB", "\xa9alb"], "Unknown Album")
            album_artist = _get_first(tags, ["albumartist", "TPE2", "aART"]) or None
            track_num = _parse_int(_get_first(tags, ["tracknumber", "TRCK"]))
            disc_num = _parse_int(_get_first(tags, ["discnumber", "TPOS"]), 1)
            year_str = _get_first(tags, ["date", "TDRC", "TYER", "\xa9day"])
            genre = _get_first(tags, ["genre", "TCON", "\xa9gen"]) or None
            sample_rate = getattr(info, "sample_rate", 44100)
            bit_depth = getattr(info, "bits_per_sample", 16)
            has_cover = False

        # Parse year
        year = None
        if year_str:
            try:
                year = int(str(year_str)[:4])
            except (ValueError, TypeError):
                pass

        duration_ms = int(info.length * 1000) if hasattr(info, "length") else 0
        channels = getattr(info, "channels", 2)

        return TrackMetadata(
            title=str(title),
            artist=str(artist),
            album=str(album),
            album_artist=str(album_artist) if album_artist else None,
            track_number=track_num,
            disc_number=disc_num,
            year=year,
            genre=str(genre) if genre else None,
            duration_ms=duration_ms,
            format=_detect_format(path),
            sample_rate=sample_rate or 44100,
            bit_depth=bit_depth or 16,
            channels=channels or 2,
            has_cover=has_cover,
        )

    except Exception:
        logger.exception("metadata_read_error", path=file_path)
        return None


def write_tags(file_path: str, *, title: str | None = None, artist: str | None = None,
               album: str | None = None) -> bool:
    """Write metadata tags to an audio file. Returns True on success."""
    path = Path(file_path)
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return False

    try:
        audio = MutagenFile(file_path)
        if audio is None:
            return False

        if isinstance(audio, FLAC):
            if title is not None:
                audio["title"] = title
            if artist is not None:
                audio["artist"] = artist
            if album is not None:
                audio["album"] = album

        elif isinstance(audio, MP3):
            from mutagen.id3 import TIT2, TPE1, TALB
            if audio.tags is None:
                audio.add_tags()
            if title is not None:
                audio.tags["TIT2"] = TIT2(encoding=3, text=[title])
            if artist is not None:
                audio.tags["TPE1"] = TPE1(encoding=3, text=[artist])
            if album is not None:
                audio.tags["TALB"] = TALB(encoding=3, text=[album])

        elif isinstance(audio, MP4):
            if title is not None:
                audio["\xa9nam"] = [title]
            if artist is not None:
                audio["\xa9ART"] = [artist]
            if album is not None:
                audio["\xa9alb"] = [album]

        elif isinstance(audio, OggVorbis):
            if title is not None:
                audio["title"] = [title]
            if artist is not None:
                audio["artist"] = [artist]
            if album is not None:
                audio["album"] = [album]

        else:
            # Generic Vorbis-comment style
            tags = audio.tags
            if tags is None:
                return False
            if title is not None:
                tags["title"] = [title]
            if artist is not None:
                tags["artist"] = [artist]
            if album is not None:
                tags["album"] = [album]

        audio.save()
        logger.info("tags_written", path=file_path, title=title, artist=artist, album=album)
        return True

    except Exception:
        logger.exception("tag_write_error", path=file_path)
        return False

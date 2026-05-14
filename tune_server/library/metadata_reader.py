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

try:
    from mutagen.dsf import DSF
except ImportError:
    DSF = None

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
    bpm: Optional[float] = None
    credits: list[dict] | None = None
    label: Optional[str] = None
    catalog_number: Optional[str] = None
    disc_subtitle: Optional[str] = None
    original_year: Optional[int] = None
    musicbrainz_recording_id: Optional[str] = None
    musicbrainz_release_id: Optional[str] = None
    musicbrainz_artist_id: Optional[str] = None
    musicbrainz_album_artist_id: Optional[str] = None
    musicbrainz_release_group_id: Optional[str] = None
    release_date: Optional[str] = None
    original_date: Optional[str] = None


def _clean_text(value: str) -> str:
    # Strip NUL bytes — PostgreSQL UTF-8 columns reject 0x00
    return value.replace("\x00", "").strip() if value else value


def _get_first(tags: dict, keys: list[str], default: str = "") -> str:
    for key in keys:
        try:
            val = tags.get(key)
        except (ValueError, KeyError):
            # Broken vorbis tags raise ValueError on .get()
            continue
        if val:
            if isinstance(val, list):
                return _clean_text(str(val[0]))
            return _clean_text(str(val))
    return default


def _parse_int(value: str, default: int = 0) -> int:
    try:
        # Handle "3/12" style track numbers
        return int(str(value).split("/")[0])
    except (ValueError, TypeError, IndexError) as e:
        logger.debug("metadata_parse_int_failed", value=str(value)[:50], error=str(e))
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


def _parse_credit_string(value: str) -> dict | None:
    """Parse a credit string like 'John Doe (guitar)' into {name, instrument}.

    Returns None if the string is empty or unparseable.
    """
    value = value.strip()
    if not value:
        return None
    # Match "Name (instrument)" pattern
    if value.endswith(")") and "(" in value:
        idx = value.rfind("(")
        name = value[:idx].strip()
        instrument = value[idx + 1:-1].strip()
        if name and instrument:
            return {"name": name, "role": "performer", "instrument": instrument}
    # No parenthesized instrument — just a name
    if value:
        return {"name": value, "role": "performer", "instrument": None}
    return None


def _safe_tag_get(tags, *keys):
    """Safely get a tag value, handling mutagen ValueError on invalid keys."""
    for key in keys:
        try:
            val = tags.get(key)
            if val:
                return val
        except (ValueError, KeyError):
            pass
    return []


def _extract_credits(audio, tags) -> list[dict]:
    """Extract credits from audio tags. Returns list of {name, role, instrument}."""
    credits: list[dict] = []

    try:
        if isinstance(audio, (FLAC, OggVorbis)):
            # PERFORMER tag: "Name (instrument)"
            performers = _safe_tag_get(tags, "performer", "PERFORMER")
            if isinstance(performers, str):
                performers = [performers]
            for perf in performers:
                parsed = _parse_credit_string(str(perf))
                if parsed:
                    credits.append(parsed)

            # COMPOSER tag -> role=composer
            composers = _safe_tag_get(tags, "composer", "COMPOSER")
            if isinstance(composers, str):
                composers = [composers]
            for comp in composers:
                name = str(comp).strip()
                if name:
                    credits.append({"name": name, "role": "composer", "instrument": None})

            # CONDUCTOR tag -> role=conductor
            conductors = _safe_tag_get(tags, "conductor", "CONDUCTOR")
            if isinstance(conductors, str):
                conductors = [conductors]
            for cond in conductors:
                name = str(cond).strip()
                if name:
                    credits.append({"name": name, "role": "conductor", "instrument": None})

            # LYRICIST tag -> role=lyricist
            lyricists = _safe_tag_get(tags, "lyricist", "LYRICIST")
            if isinstance(lyricists, str):
                lyricists = [lyricists]
            for lyr in lyricists:
                name = str(lyr).strip()
                if name:
                    credits.append({"name": name, "role": "lyricist", "instrument": None})

        elif isinstance(audio, MP3):
            # TMCL frame: musician credits list (ID3v2.4)
            for key, frame in (tags or {}).items():
                if key.startswith("TMCL"):
                    # TMCL contains pairs: [[instrument, name], ...]
                    people = getattr(frame, "people", None)
                    if people:
                        for instrument, name in people:
                            name = str(name).strip()
                            instrument = str(instrument).strip()
                            if name:
                                credits.append({
                                    "name": name,
                                    "role": "performer",
                                    "instrument": instrument or None,
                                })
                elif key.startswith("TIPL"):
                    # TIPL: involved people list (producer, mixer, etc.)
                    people = getattr(frame, "people", None)
                    if people:
                        for role, name in people:
                            name = str(name).strip()
                            role_str = str(role).strip().lower()
                            if name:
                                credits.append({
                                    "name": name,
                                    "role": role_str or "other",
                                    "instrument": None,
                                })

        # MP4: skip (no standard credit tags)

    except Exception:
        logger.debug("credit_extraction_error", exc_info=True)

    return credits


_MB_VORBIS_KEYS = {
    "recording_id": ["musicbrainz_trackid"],
    "release_id": ["musicbrainz_albumid"],
    "artist_id": ["musicbrainz_artistid"],
    "album_artist_id": ["musicbrainz_albumartistid"],
    "release_group_id": ["musicbrainz_releasegroupid"],
}
_MB_ID3_KEYS = {
    "recording_id": ["TXXX:MusicBrainz Recording Id", "TXXX:MUSICBRAINZ_TRACKID"],
    "release_id": ["TXXX:MusicBrainz Album Id", "TXXX:MUSICBRAINZ_ALBUMID"],
    "artist_id": ["TXXX:MusicBrainz Artist Id", "TXXX:MUSICBRAINZ_ARTISTID"],
    "album_artist_id": ["TXXX:MusicBrainz Album Artist Id", "TXXX:MUSICBRAINZ_ALBUMARTISTID"],
    "release_group_id": ["TXXX:MusicBrainz Release Group Id", "TXXX:MUSICBRAINZ_RELEASEGROUPID"],
}
_MB_MP4_KEYS = {
    "recording_id": ["----:com.apple.iTunes:MusicBrainz Track Id"],
    "release_id": ["----:com.apple.iTunes:MusicBrainz Album Id"],
    "artist_id": ["----:com.apple.iTunes:MusicBrainz Artist Id"],
    "album_artist_id": ["----:com.apple.iTunes:MusicBrainz Album Artist Id"],
    "release_group_id": ["----:com.apple.iTunes:MusicBrainz Release Group Id"],
}


def _extract_musicbrainz_ids(audio, tags) -> dict[str, str | None]:
    if isinstance(audio, (FLAC, OggVorbis)):
        key_map = _MB_VORBIS_KEYS
    elif isinstance(audio, MP3) or (DSF and isinstance(audio, DSF)):
        key_map = _MB_ID3_KEYS
    elif isinstance(audio, MP4):
        key_map = _MB_MP4_KEYS
    else:
        key_map = _MB_VORBIS_KEYS  # generic fallback
    return {k: (_get_first(tags, keys) or None) for k, keys in key_map.items()}


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

        # Default for label / catalog (per-branch overrides below).
        label: str | None = None
        catalog_number: str | None = None

        # Extract metadata based on file type
        if isinstance(audio, FLAC):
            title = _get_first(tags, ["title"], path.stem)
            artist = _get_first(tags, ["artist"]) or None
            album = _get_first(tags, ["album"]) or None
            album_artist = _get_first(tags, ["albumartist", "album_artist"]) or None
            track_num = _parse_int(_get_first(tags, ["tracknumber"]))
            disc_num = _parse_int(_get_first(tags, ["discnumber"]), 1)
            disc_subtitle = _get_first(tags, ["discsubtitle", "DISCSUBTITLE"]) or None
            original_year_str = _get_first(tags, ["originaldate", "originalyear", "ORIGINALDATE", "ORIGINALYEAR"])
            year_str = _get_first(tags, ["date", "year", "releasedate", "RELEASEDATE"])
            genre = _get_first(tags, ["genre"]) or None
            label = _get_first(tags, ["label", "publisher", "organization"]) or None
            catalog_number = _get_first(tags, ["catalognumber", "catalogno"]) or None
            sample_rate = info.sample_rate
            bit_depth = info.bits_per_sample or 16
            has_cover = len(audio.pictures) > 0

        elif isinstance(audio, MP3):
            title = _get_first(tags, ["TIT2"], path.stem)
            artist = _get_first(tags, ["TPE1"]) or None
            album = _get_first(tags, ["TALB"]) or None
            album_artist = _get_first(tags, ["TPE2"]) or None
            track_num = _parse_int(_get_first(tags, ["TRCK"]))
            disc_num = _parse_int(_get_first(tags, ["TPOS"]), 1)
            disc_subtitle = _get_first(tags, ["TSST", "TXXX:DISCSUBTITLE"]) or None
            original_year_str = _get_first(tags, ["TDOR"])
            year_str = _get_first(tags, ["TDRL", "TDRC", "TYER"])
            genre = _get_first(tags, ["TCON"]) or None
            label = _get_first(tags, ["TPUB"]) or None
            catalog_number = _get_first(tags, ["TXXX:CATALOGNUMBER", "TXXX:CATALOGNO"]) or None
            sample_rate = info.sample_rate
            bit_depth = 16
            has_cover = any(k.startswith("APIC") for k in tags.keys()) if tags else False

        elif isinstance(audio, MP4):
            title = _get_first(tags, ["\xa9nam"], path.stem)
            artist = _get_first(tags, ["\xa9ART"]) or None
            album = _get_first(tags, ["\xa9alb"]) or None
            album_artist = _get_first(tags, ["aART"]) or None
            trkn = tags.get("trkn", [(0, 0)])[0]
            track_num = trkn[0] if isinstance(trkn, tuple) else _parse_int(str(trkn))
            disk = tags.get("disk", [(1, 1)])[0]
            disc_num = disk[0] if isinstance(disk, tuple) else 1
            disc_subtitle = _get_first(tags, ["----:com.apple.iTunes:DISCSUBTITLE"]) or None
            original_year_str = ""
            year_str = _get_first(tags, ["\xa9day"])
            genre = _get_first(tags, ["\xa9gen"]) or None
            label = _get_first(tags, ["----:com.apple.iTunes:LABEL"]) or None
            catalog_number = _get_first(tags, ["----:com.apple.iTunes:CATALOGNUMBER"]) or None
            sample_rate = info.sample_rate
            bit_depth = info.bits_per_sample if hasattr(info, "bits_per_sample") else 16
            has_cover = "covr" in tags

        elif isinstance(audio, OggVorbis):
            title = _get_first(tags, ["title"], path.stem)
            artist = _get_first(tags, ["artist"]) or None
            album = _get_first(tags, ["album"]) or None
            album_artist = _get_first(tags, ["albumartist"]) or None
            track_num = _parse_int(_get_first(tags, ["tracknumber"]))
            disc_num = _parse_int(_get_first(tags, ["discnumber"]), 1)
            disc_subtitle = _get_first(tags, ["discsubtitle", "DISCSUBTITLE"]) or None
            original_year_str = _get_first(tags, ["originaldate", "originalyear", "ORIGINALDATE", "ORIGINALYEAR"])
            year_str = _get_first(tags, ["date", "releasedate", "RELEASEDATE"])
            genre = _get_first(tags, ["genre"]) or None
            label = _get_first(tags, ["label", "publisher", "organization"]) or None
            catalog_number = _get_first(tags, ["catalognumber", "catalogno"]) or None
            sample_rate = info.sample_rate
            bit_depth = 16
            has_cover = False

        elif DSF and isinstance(audio, DSF):
            # DSD files (DSF/DFF) — sample_rate is the DSD rate (2822400, 5644800, etc.)
            title = _get_first(tags, ["TIT2"], path.stem) if tags else path.stem
            artist = (_get_first(tags, ["TPE1"]) or None) if tags else None
            album = (_get_first(tags, ["TALB"]) or None) if tags else None
            album_artist = (_get_first(tags, ["TPE2"]) or None) if tags else None
            track_num = _parse_int(_get_first(tags, ["TRCK"])) if tags else 0
            disc_num = _parse_int(_get_first(tags, ["TPOS"]), 1) if tags else 1
            disc_subtitle = (_get_first(tags, ["TSST", "TXXX:DISCSUBTITLE"]) or None) if tags else None
            original_year_str = (_get_first(tags, ["TDOR"]) if tags else "")
            year_str = (_get_first(tags, ["TDRL", "TDRC", "TYER"]) if tags else "")
            genre = (_get_first(tags, ["TCON"]) or None) if tags else None
            label = (_get_first(tags, ["TPUB"]) or None) if tags else None
            catalog_number = (_get_first(tags, ["TXXX:CATALOGNUMBER", "TXXX:CATALOGNO"]) or None) if tags else None
            sample_rate = info.sample_rate  # 2822400 (DSD64), 5644800 (DSD128), etc.
            bit_depth = 1  # DSD is 1-bit
            has_cover = any(k.startswith("APIC") for k in tags.keys()) if tags else False

        else:
            # Generic fallback
            title = _get_first(tags, ["title", "TIT2", "\xa9nam"], path.stem)
            artist = _get_first(tags, ["artist", "TPE1", "\xa9ART"]) or None
            album = _get_first(tags, ["album", "TALB", "\xa9alb"]) or None
            album_artist = _get_first(tags, ["albumartist", "TPE2", "aART"]) or None
            track_num = _parse_int(_get_first(tags, ["tracknumber", "TRCK"]))
            disc_num = _parse_int(_get_first(tags, ["discnumber", "TPOS"]), 1)
            disc_subtitle = _get_first(tags, [
                "discsubtitle", "DISCSUBTITLE", "TSST", "TXXX:DISCSUBTITLE",
                "----:com.apple.iTunes:DISCSUBTITLE",
            ]) or None
            original_year_str = _get_first(tags, [
                "originaldate", "ORIGINALDATE", "originalyear", "ORIGINALYEAR", "TDOR",
            ])
            year_str = _get_first(tags, ["date", "releasedate", "RELEASEDATE", "TDRL", "TDRC", "TYER", "\xa9day"])
            genre = _get_first(tags, ["genre", "TCON", "\xa9gen"]) or None
            # Label/publisher: VorbisComment uses LABEL/PUBLISHER/ORGANIZATION,
            # ID3v2 uses TPUB, MP4 has its own iTunes atom but mutagen
            # surfaces it as 'label' in EasyMP4. Catalog number similar.
            label = _get_first(tags, [
                "label", "LABEL", "publisher", "PUBLISHER",
                "organization", "ORGANIZATION", "TPUB",
                "----:com.apple.iTunes:LABEL",
            ]) or None
            catalog_number = _get_first(tags, [
                "catalognumber", "CATALOGNUMBER", "catalogno", "CATALOGNO",
                "TXXX:CATALOGNUMBER", "TXXX:CATALOGNO",
                "----:com.apple.iTunes:CATALOGNUMBER",
            ]) or None
            sample_rate = getattr(info, "sample_rate", 44100)
            bit_depth = getattr(info, "bits_per_sample", 16)
            has_cover = False

        # Parse years
        year = None
        if year_str:
            try:
                year = int(str(year_str)[:4])
            except (ValueError, TypeError) as e:
                logger.debug("metadata_year_parse_failed", value=str(year_str)[:20], error=str(e))
        original_year = None
        if original_year_str:
            try:
                original_year = int(str(original_year_str)[:4])
            except (ValueError, TypeError):
                pass

        release_date = str(year_str).strip() if year_str and len(str(year_str).strip()) > 4 else None
        original_date = str(original_year_str).strip() if original_year_str and len(str(original_year_str).strip()) > 4 else None

        duration_ms = int(info.length * 1000) if hasattr(info, "length") else 0
        channels = getattr(info, "channels", 2)

        # Extract credits from tags
        extracted_credits = _extract_credits(audio, tags)

        # Extract BPM from tags
        bpm_value = None
        try:
            if isinstance(audio, MP3):
                raw_bpm = _get_first(tags, ["TBPM"]) or None
            elif isinstance(audio, (FLAC, OggVorbis)):
                raw_bpm = _get_first(tags, ["BPM", "bpm"]) or None
            elif isinstance(audio, MP4):
                tmpo = tags.get("tmpo", [None])
                raw_bpm = tmpo[0] if tmpo else None
            else:
                raw_bpm = _get_first(tags, ["BPM", "bpm", "TBPM"]) or None
            if raw_bpm is not None:
                bpm_value = float(str(raw_bpm).strip())
                if bpm_value <= 0 or bpm_value > 999:
                    bpm_value = None
        except (ValueError, TypeError):
            bpm_value = None

        mb_ids = _extract_musicbrainz_ids(audio, tags)

        return TrackMetadata(
            title=str(title) if title else "",
            artist=str(artist) if artist else None,
            album=str(album) if album else None,
            album_artist=str(album_artist) if album_artist else None,
            track_number=track_num,
            disc_number=disc_num,
            year=year,
            original_year=original_year,
            genre=str(genre) if genre else None,
            duration_ms=duration_ms,
            format=_detect_format(path),
            sample_rate=sample_rate or 44100,
            bit_depth=bit_depth or 16,
            channels=channels or 2,
            has_cover=has_cover,
            label=_clean_text(str(label)) if label else None,
            catalog_number=_clean_text(str(catalog_number)) if catalog_number else None,
            bpm=bpm_value,
            credits=extracted_credits if extracted_credits else None,
            disc_subtitle=_clean_text(str(disc_subtitle)) if disc_subtitle else None,
            musicbrainz_recording_id=mb_ids.get("recording_id"),
            musicbrainz_release_id=mb_ids.get("release_id"),
            musicbrainz_artist_id=mb_ids.get("artist_id"),
            musicbrainz_album_artist_id=mb_ids.get("album_artist_id"),
            musicbrainz_release_group_id=mb_ids.get("release_group_id"),
            release_date=release_date,
            original_date=original_date,
        )

    except Exception:
        logger.exception("metadata_read_error", path=file_path)
        return None


def write_tags(file_path: str, *, title: str | None = None, artist: str | None = None,
               album: str | None = None, album_artist: str | None = None,
               genre: str | None = None, year: str | None = None,
               track_number: int | None = None, disc_number: int | None = None) -> bool:
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
            if album_artist is not None:
                audio["albumartist"] = album_artist
            if genre is not None:
                audio["genre"] = genre
            if year is not None:
                audio["date"] = year
            if track_number is not None:
                audio["tracknumber"] = str(track_number)
            if disc_number is not None:
                audio["discnumber"] = str(disc_number)

        elif isinstance(audio, MP3):
            from mutagen.id3 import TIT2, TPE1, TPE2, TALB, TCON, TDRC, TRCK, TPOS
            if audio.tags is None:
                audio.add_tags()
            if title is not None:
                audio.tags["TIT2"] = TIT2(encoding=3, text=[title])
            if artist is not None:
                audio.tags["TPE1"] = TPE1(encoding=3, text=[artist])
            if album is not None:
                audio.tags["TALB"] = TALB(encoding=3, text=[album])
            if album_artist is not None:
                audio.tags["TPE2"] = TPE2(encoding=3, text=[album_artist])
            if genre is not None:
                audio.tags["TCON"] = TCON(encoding=3, text=[genre])
            if year is not None:
                audio.tags["TDRC"] = TDRC(encoding=3, text=[year])
            if track_number is not None:
                audio.tags["TRCK"] = TRCK(encoding=3, text=[str(track_number)])
            if disc_number is not None:
                audio.tags["TPOS"] = TPOS(encoding=3, text=[str(disc_number)])

        elif isinstance(audio, MP4):
            if title is not None:
                audio["\xa9nam"] = [title]
            if artist is not None:
                audio["\xa9ART"] = [artist]
            if album is not None:
                audio["\xa9alb"] = [album]
            if album_artist is not None:
                audio["aART"] = [album_artist]
            if genre is not None:
                audio["\xa9gen"] = [genre]
            if year is not None:
                audio["\xa9day"] = [year]
            if track_number is not None:
                audio["trkn"] = [(track_number, 0)]
            if disc_number is not None:
                audio["disk"] = [(disc_number, 0)]

        elif isinstance(audio, OggVorbis):
            if title is not None:
                audio["title"] = [title]
            if artist is not None:
                audio["artist"] = [artist]
            if album is not None:
                audio["album"] = [album]
            if album_artist is not None:
                audio["albumartist"] = [album_artist]
            if genre is not None:
                audio["genre"] = [genre]
            if year is not None:
                audio["date"] = [year]
            if track_number is not None:
                audio["tracknumber"] = [str(track_number)]
            if disc_number is not None:
                audio["discnumber"] = [str(disc_number)]

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
            if album_artist is not None:
                tags["albumartist"] = [album_artist]
            if genre is not None:
                tags["genre"] = [genre]
            if year is not None:
                tags["date"] = [year]
            if track_number is not None:
                tags["tracknumber"] = [str(track_number)]
            if disc_number is not None:
                tags["discnumber"] = [str(disc_number)]

        audio.save()
        logger.info("tags_written", path=file_path, title=title, artist=artist, album=album)
        return True

    except Exception:
        logger.exception("tag_write_error", path=file_path)
        return False

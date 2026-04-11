"""Tag writer — write metadata to audio files using Mutagen.

Supports ID3 (MP3), Vorbis Comments (FLAC, OGG, OPUS), MP4/M4A tags.
Only writes to file when explicitly requested (à la demande).
"""

from __future__ import annotations

from pathlib import Path

import structlog

logger = structlog.get_logger()

# Field mapping: DB field → tag name per format
_ID3_MAP = {
    "title": "TIT2",
    "artist_name": "TPE1",
    "album_title": "TALB",
    "track_number": "TRCK",
    "disc_number": "TPOS",
    "genre": "TCON",
    "composer": "TCOM",
    "year": "TDRC",
    "lyrics": "USLT",
    "comment": "COMM",
    "isrc": "TSRC",
    "bpm": "TBPM",
    "label": "TPUB",
    "musicbrainz_recording_id": "TXXX:MusicBrainz Recording Id",
}

_VORBIS_MAP = {
    "title": "TITLE",
    "artist_name": "ARTIST",
    "album_title": "ALBUM",
    "track_number": "TRACKNUMBER",
    "disc_number": "DISCNUMBER",
    "genre": "GENRE",
    "composer": "COMPOSER",
    "year": "DATE",
    "lyrics": "LYRICS",
    "comment": "COMMENT",
    "isrc": "ISRC",
    "bpm": "BPM",
    "label": "LABEL",
    "musicbrainz_recording_id": "MUSICBRAINZ_TRACKID",
}

_MP4_MAP = {
    "title": "\xa9nam",
    "artist_name": "\xa9ART",
    "album_title": "\xa9alb",
    "genre": "\xa9gen",
    "composer": "\xa9wrt",
    "year": "\xa9day",
    "comment": "\xa9cmt",
}


async def write_tags(file_path: str, metadata: dict) -> dict:
    """Write metadata tags to an audio file.

    Args:
        file_path: Path to the audio file
        metadata: Dict of field_name → value to write

    Returns:
        {"ok": True, "fields_written": N} or {"ok": False, "error": str}
    """
    import mutagen
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TPOS, TCON, TCOM, TDRC, TSRC, TBPM, TPUB
    from mutagen.id3 import USLT, COMM, TXXX

    path = Path(file_path)
    if not path.exists():
        return {"ok": False, "error": f"File not found: {file_path}"}

    try:
        audio = mutagen.File(file_path, easy=False)
        if audio is None:
            return {"ok": False, "error": "Unsupported format"}

        fields_written = 0
        file_type = type(audio).__name__.lower()

        if hasattr(audio, 'tags') and audio.tags is None:
            audio.add_tags()

        # Determine format
        is_id3 = "mp3" in file_type or "id3" in file_type or hasattr(audio, 'ID3')
        is_mp4 = "mp4" in file_type or "m4a" in file_type or "aac" in file_type
        is_vorbis = "flac" in file_type or "ogg" in file_type or "opus" in file_type

        for field, value in metadata.items():
            if value is None:
                continue

            str_value = str(value)

            if is_id3:
                tag_name = _ID3_MAP.get(field)
                if not tag_name:
                    continue
                # ID3 frames
                frame_map = {
                    "TIT2": TIT2, "TPE1": TPE1, "TALB": TALB, "TCON": TCON,
                    "TCOM": TCOM, "TDRC": TDRC, "TSRC": TSRC, "TBPM": TBPM, "TPUB": TPUB,
                }
                if tag_name == "TRCK":
                    audio.tags.add(TRCK(encoding=3, text=[str_value]))
                elif tag_name == "TPOS":
                    audio.tags.add(TPOS(encoding=3, text=[str_value]))
                elif tag_name == "USLT":
                    audio.tags.add(USLT(encoding=3, lang="eng", text=str_value))
                elif tag_name == "COMM":
                    audio.tags.add(COMM(encoding=3, lang="eng", text=[str_value]))
                elif tag_name.startswith("TXXX:"):
                    desc = tag_name.split(":", 1)[1]
                    audio.tags.add(TXXX(encoding=3, desc=desc, text=[str_value]))
                elif tag_name in frame_map:
                    audio.tags.add(frame_map[tag_name](encoding=3, text=[str_value]))
                else:
                    continue
                fields_written += 1

            elif is_vorbis:
                tag_name = _VORBIS_MAP.get(field)
                if tag_name:
                    audio.tags[tag_name] = [str_value]
                    fields_written += 1

            elif is_mp4:
                tag_name = _MP4_MAP.get(field)
                if tag_name:
                    audio.tags[tag_name] = [str_value]
                    fields_written += 1

        if fields_written > 0:
            audio.save()
            logger.info("tags_written", file=file_path, fields=fields_written)

        return {"ok": True, "fields_written": fields_written}

    except Exception as e:
        logger.exception("tag_write_error", file=file_path)
        return {"ok": False, "error": str(e)}


async def read_tags(file_path: str) -> dict:
    """Read all metadata tags from an audio file.

    Returns dict of field_name → value.
    """
    import mutagen

    path = Path(file_path)
    if not path.exists():
        return {}

    try:
        audio = mutagen.File(file_path, easy=True)
        if audio is None:
            return {}

        result = {}
        easy_map = {
            "title": "title", "artist": "artist_name", "album": "album_title",
            "tracknumber": "track_number", "discnumber": "disc_number",
            "genre": "genre", "composer": "composer", "date": "year",
            "isrc": "isrc", "bpm": "bpm",
        }
        for easy_key, field in easy_map.items():
            val = audio.get(easy_key)
            if val:
                result[field] = val[0] if isinstance(val, list) else str(val)

        return result
    except Exception:
        return {}

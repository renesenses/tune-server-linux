"""Audio file tagging using mutagen."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

try:
    import mutagen
    from mutagen.flac import FLAC, Picture
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TDRC, APIC
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False
    logger.warning("mutagen_not_installed", hint="pip install mutagen for audio tagging")


async def tag_file(
    file_path: Path,
    *,
    title: str = "",
    artist: str = "",
    album: str = "",
    track_number: Optional[int] = None,
    year: Optional[int] = None,
    cover_data: Optional[bytes] = None,
) -> bool:
    """Tag an audio file with metadata. Returns True on success."""
    if not HAS_MUTAGEN:
        return False

    if not file_path.exists():
        return False

    ext = file_path.suffix.lower()

    try:
        if ext == ".flac":
            return _tag_flac(file_path, title, artist, album, track_number, year, cover_data)
        elif ext == ".mp3":
            return _tag_mp3(file_path, title, artist, album, track_number, year, cover_data)
        elif ext in (".m4a", ".aac", ".mp4"):
            return _tag_m4a(file_path, title, artist, album, track_number, year, cover_data)
        elif ext in (".ogg", ".opus"):
            return _tag_ogg(file_path, title, artist, album, track_number, year, cover_data)
        else:
            logger.debug("tagging_unsupported_format", ext=ext)
            return False
    except Exception:
        logger.exception("tagging_error", path=str(file_path))
        return False


def _tag_flac(path, title, artist, album, track_num, year, cover_data):
    audio = FLAC(str(path))
    if title: audio["TITLE"] = title
    if artist: audio["ARTIST"] = artist
    if album: audio["ALBUM"] = album
    if track_num: audio["TRACKNUMBER"] = str(track_num)
    if year: audio["DATE"] = str(year)
    if cover_data:
        pic = Picture()
        pic.type = 3  # Front cover
        pic.mime = "image/jpeg"
        pic.data = cover_data
        audio.clear_pictures()
        audio.add_picture(pic)
    audio.save()
    return True


def _tag_mp3(path, title, artist, album, track_num, year, cover_data):
    try:
        audio = MP3(str(path), ID3=ID3)
    except mutagen.id3.ID3NoHeaderError:
        audio = MP3(str(path))
        audio.add_tags()
    if title: audio.tags.add(TIT2(encoding=3, text=[title]))
    if artist: audio.tags.add(TPE1(encoding=3, text=[artist]))
    if album: audio.tags.add(TALB(encoding=3, text=[album]))
    if track_num: audio.tags.add(TRCK(encoding=3, text=[str(track_num)]))
    if year: audio.tags.add(TDRC(encoding=3, text=[str(year)]))
    if cover_data:
        audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, data=cover_data))
    audio.save()
    return True


def _tag_m4a(path, title, artist, album, track_num, year, cover_data):
    audio = MP4(str(path))
    if title: audio["\xa9nam"] = [title]
    if artist: audio["\xa9ART"] = [artist]
    if album: audio["\xa9alb"] = [album]
    if track_num: audio["trkn"] = [(track_num, 0)]
    if year: audio["\xa9day"] = [str(year)]
    if cover_data:
        audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()
    return True


def _tag_ogg(path, title, artist, album, track_num, year, cover_data):
    ext = Path(path).suffix.lower()
    if ext == ".opus":
        audio = OggOpus(str(path))
    else:
        audio = OggVorbis(str(path))
    if title: audio["TITLE"] = title
    if artist: audio["ARTIST"] = artist
    if album: audio["ALBUM"] = album
    if track_num: audio["TRACKNUMBER"] = str(track_num)
    if year: audio["DATE"] = str(year)
    # Cover art in Ogg is complex (METADATA_BLOCK_PICTURE), skip for now
    audio.save()
    return True


async def download_cover(url: str) -> Optional[bytes]:
    """Download cover art from URL."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception:
        pass
    return None

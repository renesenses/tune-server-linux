from __future__ import annotations

import hashlib
import json
import subprocess
import time
from io import BytesIO
from pathlib import Path
from typing import Optional

import structlog
from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from PIL import Image

from tune_server.config import settings

logger = structlog.get_logger()

_MUSICBRAINZ_USER_AGENT = "TuneServer/0.1.0 (contact@example.com)"
_last_musicbrainz_request: float = 0.0


def _get_cache_dir() -> Path:
    cache_dir = Path(settings.artwork_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _hash_path(file_path: str) -> str:
    return hashlib.md5(file_path.encode()).hexdigest()


def extract_cover_art(file_path: str) -> Optional[bytes]:
    try:
        audio = MutagenFile(file_path)
        if audio is None:
            return None

        if isinstance(audio, FLAC):
            if audio.pictures:
                return audio.pictures[0].data

        elif isinstance(audio, MP3):
            tags = audio.tags or {}
            for key in tags:
                if key.startswith("APIC"):
                    return tags[key].data

        elif isinstance(audio, MP4):
            covr = (audio.tags or {}).get("covr")
            if covr:
                return bytes(covr[0])

        # Try generic approach: look for embedded pictures directory
        folder = Path(file_path).parent
        for name in ("cover.jpg", "cover.png", "folder.jpg", "folder.png",
                      "front.jpg", "front.png", "album.jpg", "album.png"):
            art_path = folder / name
            if art_path.exists():
                return art_path.read_bytes()

    except Exception:
        logger.exception("cover_art_extraction_error", path=file_path)

    return None


def save_artwork(file_path: str, image_data: bytes) -> Optional[str]:
    try:
        cache_dir = _get_cache_dir()
        hash_name = _hash_path(file_path)
        output_path = cache_dir / f"{hash_name}.jpg"

        if output_path.exists():
            return str(output_path)

        img = Image.open(BytesIO(image_data))
        img = img.convert("RGB")

        max_size = settings.artwork_max_size
        if img.width > max_size or img.height > max_size:
            img.thumbnail((max_size, max_size), Image.LANCZOS)

        img.save(output_path, "JPEG", quality=90)
        return str(output_path)

    except Exception:
        logger.exception("artwork_save_error", path=file_path)
        return None


def get_album_artwork(file_path: str) -> Optional[str]:
    cache_dir = _get_cache_dir()
    hash_name = _hash_path(file_path)
    cached = cache_dir / f"{hash_name}.jpg"

    if cached.exists():
        return str(cached)

    image_data = extract_cover_art(file_path)
    if image_data:
        return save_artwork(file_path, image_data)

    return None


def _musicbrainz_rate_limit() -> None:
    """Enforce 1 request/second rate limit for MusicBrainz API."""
    global _last_musicbrainz_request
    now = time.monotonic()
    elapsed = now - _last_musicbrainz_request
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_musicbrainz_request = time.monotonic()


def fetch_cover_from_musicbrainz(
    artist_name: str,
    album_title: str,
    cache_dir: Optional[str] = None,
) -> Optional[str]:
    """Look up album cover via MusicBrainz + Cover Art Archive.

    Returns the cached file path on success, None otherwise.
    """
    if not artist_name or not album_title:
        return None

    cache_path = Path(cache_dir) if cache_dir else _get_cache_dir()
    cache_path.mkdir(parents=True, exist_ok=True)

    # Use a stable hash based on artist+album so the same lookup reuses cache
    cache_key = hashlib.md5(f"mb:{artist_name}:{album_title}".encode()).hexdigest()
    output_path = cache_path / f"{cache_key}.jpg"
    if output_path.exists():
        return str(output_path)

    try:
        # Step 1: Search MusicBrainz for the release
        _musicbrainz_rate_limit()
        query = f'artist:"{artist_name}" AND release:"{album_title}"'
        search_url = "https://musicbrainz.org/ws/2/release/"
        result = subprocess.run(
            ["curl", "-4sf", "--max-time", "15",
             "-H", f"User-Agent: {_MUSICBRAINZ_USER_AGENT}",
             "-G", search_url,
             "--data-urlencode", f"query={query}",
             "--data-urlencode", "fmt=json",
             "--data-urlencode", "limit=1"],
            capture_output=True, timeout=20,
        )
        if result.returncode != 0:
            logger.debug("musicbrainz_search_failed", artist=artist_name, album=album_title)
            return None

        data = json.loads(result.stdout)
        releases = data.get("releases", [])
        if not releases:
            logger.debug("musicbrainz_no_result", artist=artist_name, album=album_title)
            return None

        mbid = releases[0]["id"]

        # Step 2: Fetch cover from Cover Art Archive
        _musicbrainz_rate_limit()
        cover_url = f"https://coverartarchive.org/release/{mbid}/front-500"
        cover_result = subprocess.run(
            ["curl", "-4sfL", "--max-time", "20",
             "-H", f"User-Agent: {_MUSICBRAINZ_USER_AGENT}",
             "-o", str(output_path), cover_url],
            capture_output=True, timeout=25,
        )
        if cover_result.returncode != 0 or not output_path.exists():
            logger.debug("musicbrainz_no_cover", artist=artist_name, album=album_title)
            output_path.unlink(missing_ok=True)
            return None

        # Step 3: Resize if needed
        img = Image.open(output_path)
        img = img.convert("RGB")

        max_size = settings.artwork_max_size
        if img.width > max_size or img.height > max_size:
            img.thumbnail((max_size, max_size), Image.LANCZOS)

        img.save(output_path, "JPEG", quality=90)
        logger.info("musicbrainz_cover_fetched", artist=artist_name, album=album_title, mbid=mbid)
        return str(output_path)

    except Exception:
        logger.exception("musicbrainz_cover_error", artist=artist_name, album=album_title)
        output_path.unlink(missing_ok=True)

    return None

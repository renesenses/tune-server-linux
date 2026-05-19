from __future__ import annotations

import hashlib
import json
import subprocess
import time
from io import BytesIO
from pathlib import Path
from typing import Optional

from tune_server.utils.network import subprocess_hide_window

import structlog
from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from PIL import Image, ImageFile

# Tolerate truncated JPEG/PNG payloads embedded in audio files. Many MP3
# collections have ID3 APIC frames where trailing bytes were cut by a
# buggy tagger; without this flag PIL raises OSError("image file is
# truncated") on every such file and we log one error per track during
# scan. With it, PIL still renders what it can and we get a usable
# (slightly cropped) thumbnail. Reported by Yves on 2026-04-30 during
# his v0.6.5 → v0.7.56 upgrade rescan.
ImageFile.LOAD_TRUNCATED_IMAGES = True

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


def _extract_wavpack_cover(file_path: str) -> Optional[bytes]:
    """Extract cover art from a WavPack file whose audio-info block mutagen
    cannot parse (e.g. 384 kHz custom-rate files).  Reads APEv2 binary items
    ('Cover Art (Front)' etc.) directly, without touching stream info.
    Falls back to folder image files if no embedded art is found."""
    try:
        from mutagen.apev2 import APEv2, APENoHeaderError
        ape = APEv2(file_path)
        for key in ape:
            if "cover art" in key.lower():
                raw = bytes(ape[key])
                # APEv2 binary cover: <filename>\x00<image bytes>
                nul = raw.find(b"\x00")
                if nul != -1:
                    data = raw[nul + 1:]
                    if data:
                        return data
                elif raw:
                    return raw
    except Exception:
        pass

    # Folder image fallback
    _cover_names = {"cover", "folder", "front", "album", "artwork", "thumb"}
    _cover_exts = {".jpg", ".jpeg", ".png", ".webp"}
    try:
        for child in Path(file_path).parent.iterdir():
            if child.is_file() and child.stem.lower() in _cover_names and child.suffix.lower() in _cover_exts:
                return child.read_bytes()
    except Exception:
        pass
    return None


def extract_cover_art(file_path: str) -> Optional[bytes]:
    try:
        try:
            audio = MutagenFile(file_path)
        except IndexError:
            # mutagen's WavPack RATES table doesn't cover non-standard sample
            # rates (e.g. 384 kHz).  Try APEv2 binary cover art, then folder.
            if Path(file_path).suffix.lower() == ".wv":
                return _extract_wavpack_cover(file_path)
            raise
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

        else:
            tags = audio.tags or {}
            if hasattr(tags, "getall"):
                pics = tags.getall("APIC")
                if pics:
                    return pics[0].data
            else:
                for key in tags:
                    if str(key).startswith("APIC"):
                        return tags[key].data

        # Try generic approach: look for cover image files in folder
        folder = Path(file_path).parent
        _cover_names = {"cover", "folder", "front", "album", "artwork", "thumb"}
        _cover_exts = {".jpg", ".jpeg", ".png", ".webp"}
        for child in folder.iterdir():
            if child.is_file() and child.stem.lower() in _cover_names and child.suffix.lower() in _cover_exts:
                return child.read_bytes()

    except Exception:
        logger.exception("cover_art_extraction_error", path=file_path)

    return None


def save_artwork(file_path: str, image_data: bytes, force: bool = False) -> Optional[str]:
    try:
        cache_dir = _get_cache_dir()
        hash_name = _hash_path(file_path)
        output_path = cache_dir / f"{hash_name}.jpg"

        if output_path.exists() and not force:
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


def copy_cover_to_album_folder(cover_cache_path: str, track_file_path: str) -> Optional[str]:
    """Copy a cover image as cover.jpg into the album's music folder.

    Returns the destination path on success, None otherwise.
    """
    try:
        src = Path(cover_cache_path)
        if not src.is_absolute():
            # artwork_cache paths are relative to cwd — resolve via cache dir
            cache_dir = _get_cache_dir()
            candidate = cache_dir / src.name
            if candidate.exists():
                src = candidate
            else:
                src = src.resolve()
        if not src.exists():
            return None

        dest_dir = Path(track_file_path).parent
        if not dest_dir.exists():
            return None

        dest = dest_dir / "cover.jpg"
        if dest.exists():
            return str(dest)  # already there

        img = Image.open(src)
        img = img.convert("RGB")
        img.save(dest, "JPEG", quality=92)
        logger.info("cover_copied_to_folder", dest=str(dest))
        return str(dest)

    except Exception:
        logger.exception("copy_cover_to_folder_error", src=cover_cache_path, track=track_file_path)
        return None


def _musicbrainz_rate_limit() -> None:
    """Enforce 1 request/second rate limit for MusicBrainz API."""
    global _last_musicbrainz_request
    now = time.monotonic()
    elapsed = now - _last_musicbrainz_request
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_musicbrainz_request = time.monotonic()


def cache_cover_url(url: str) -> Optional[str]:
    """Download and cache an HTTP cover URL locally.

    Used for streaming service covers (Qobuz, Tidal) whose CDN URLs expire.
    Returns the local cached file path, or None on failure.
    The cache key is a stable MD5 hash of the URL (without query params).
    """
    if not url or not url.startswith("http"):
        return None

    # Strip query params for a stable cache key (CDN signatures change)
    base_url = url.split("?")[0]
    cache_key = hashlib.md5(base_url.encode()).hexdigest()
    cache_dir = _get_cache_dir()
    output_path = cache_dir / f"{cache_key}.jpg"

    if output_path.exists():
        return str(output_path)

    try:
        downloaded = False
        try:
            result = subprocess.run(
                ["curl", "-4sfL", "--max-time", "10",
                 "-A", "Mozilla/5.0 (Tune Server)",
                 "-o", str(output_path), url],
                capture_output=True, timeout=15, **subprocess_hide_window(),
            )
            downloaded = result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0
        except FileNotFoundError:
            pass

        if not downloaded:
            output_path.unlink(missing_ok=True)
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Tune Server)"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            if not data:
                logger.debug("cache_cover_url_download_failed", url=base_url)
                return None
            output_path.write_bytes(data)

        img = Image.open(output_path)
        img = img.convert("RGB")
        max_size = settings.artwork_max_size
        if img.width > max_size or img.height > max_size:
            img.thumbnail((max_size, max_size), Image.LANCZOS)
        img.save(output_path, "JPEG", quality=90)

        logger.info("cover_url_cached", url=base_url, path=str(output_path))
        return str(output_path)

    except Exception:
        logger.debug("cache_cover_url_error", url=base_url)
        output_path.unlink(missing_ok=True)
        return None


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
            capture_output=True, timeout=20, **subprocess_hide_window(),
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
            capture_output=True, timeout=25, **subprocess_hide_window(),
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

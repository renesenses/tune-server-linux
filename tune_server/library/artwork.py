from __future__ import annotations

import hashlib
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

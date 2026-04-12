"""Cover art fetcher — Cover Art Archive + Discogs."""

from __future__ import annotations

import hashlib
from pathlib import Path

import aiohttp
import structlog

logger = structlog.get_logger()

CAA_URL = "https://coverartarchive.org/release"
DISCOGS_API = "https://api.discogs.com"
DISCOGS_UA = "TuneServer/0.5.8"


async def fetch_cover_from_caa(musicbrainz_release_id: str, cache_dir: str) -> str | None:
    """Fetch cover from Cover Art Archive by MusicBrainz release ID.

    Returns local file path or None.
    """
    if not musicbrainz_release_id:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            # Try front cover
            url = f"{CAA_URL}/{musicbrainz_release_id}/front-500"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15),
                                   allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                img_data = await resp.read()
                if len(img_data) < 1000:
                    return None

        # Save to cache
        h = hashlib.md5(musicbrainz_release_id.encode()).hexdigest()
        cache_path = Path(cache_dir) / f"caa_{h}.jpg"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(img_data)
        logger.info("cover_fetched_caa", release_id=musicbrainz_release_id, path=str(cache_path))
        return str(cache_path)

    except Exception:
        logger.debug("cover_caa_error", release_id=musicbrainz_release_id)
        return None


async def fetch_cover_from_discogs(
    album_title: str,
    artist_name: str,
    discogs_token: str,
    cache_dir: str,
) -> str | None:
    """Search Discogs for album cover art.

    Returns local file path or None.
    """
    if not discogs_token or not album_title:
        return None

    headers = {
        "Authorization": f"Discogs token={discogs_token}",
        "User-Agent": DISCOGS_UA,
    }

    try:
        async with aiohttp.ClientSession(headers=headers,
                                          timeout=aiohttp.ClientTimeout(total=15)) as session:
            # Search for release
            query = f"{artist_name} {album_title}" if artist_name else album_title
            async with session.get(
                f"{DISCOGS_API}/database/search",
                params={"q": query, "type": "release", "per_page": 1},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                results = data.get("results", [])
                if not results:
                    return None

                cover_url = results[0].get("cover_image") or results[0].get("thumb")
                if not cover_url or "spacer.gif" in cover_url:
                    return None

            # Download image
            async with session.get(cover_url) as img_resp:
                if img_resp.status != 200:
                    return None
                img_data = await img_resp.read()
                if len(img_data) < 1000:
                    return None

        # Save to cache
        h = hashlib.md5(cover_url.encode()).hexdigest()
        cache_path = Path(cache_dir) / f"discogs_{h}.jpg"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(img_data)
        logger.info("cover_fetched_discogs", album=album_title, path=str(cache_path))
        return str(cache_path)

    except Exception:
        logger.debug("cover_discogs_error", album=album_title)
        return None


async def search_covers(
    album_title: str,
    artist_name: str = "",
    musicbrainz_release_id: str = "",
    discogs_token: str = "",
    cache_dir: str = "artwork_cache",
) -> list[dict]:
    """Search for covers from multiple sources.

    Returns list of {source, url, local_path}.
    """
    results = []

    # Cover Art Archive
    if musicbrainz_release_id:
        path = await fetch_cover_from_caa(musicbrainz_release_id, cache_dir)
        if path:
            results.append({"source": "coverartarchive", "local_path": path})

    # Discogs
    if discogs_token:
        path = await fetch_cover_from_discogs(album_title, artist_name, discogs_token, cache_dir)
        if path:
            results.append({"source": "discogs", "local_path": path})

    return results

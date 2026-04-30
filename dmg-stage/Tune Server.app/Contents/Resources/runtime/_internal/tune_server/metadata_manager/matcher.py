"""MusicBrainz metadata lookup — search by title/artist/album."""

from __future__ import annotations

import asyncio

import aiohttp
import structlog

logger = structlog.get_logger()

MB_API = "https://musicbrainz.org/ws/2"
MB_UA = "TuneServer/0.5.8 (https://github.com/renesenses/tune-server-linux)"
MB_RATE_LIMIT = 1.0  # 1 request per second


async def lookup_track(title: str, artist: str, album: str = "") -> list[dict]:
    """Search MusicBrainz for a track by title + artist.

    Returns list of candidates with: title, artist, album, isrc,
    musicbrainz_recording_id, year, label.
    """
    query_parts = []
    if title:
        query_parts.append(f'recording:"{title}"')
    if artist:
        query_parts.append(f'artist:"{artist}"')
    if album:
        query_parts.append(f'release:"{album}"')

    query = " AND ".join(query_parts)
    if not query:
        return []

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{MB_API}/recording",
                params={"query": query, "limit": 5, "fmt": "json"},
                headers={"User-Agent": MB_UA},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

        results = []
        for rec in data.get("recordings", []):
            # Extract ISRC
            isrcs = rec.get("isrcs", [])

            # Extract artist
            artist_credit = rec.get("artist-credit", [])
            artist_name = " ".join(ac.get("name", "") for ac in artist_credit) if artist_credit else ""

            # Extract release info (album, year, label)
            releases = rec.get("releases", [])
            album_title = ""
            year = None
            label = ""
            if releases:
                rel = releases[0]
                album_title = rel.get("title", "")
                date = rel.get("date", "")
                if date and len(date) >= 4:
                    try:
                        year = int(date[:4])
                    except ValueError:
                        pass
                label_info = rel.get("label-info", [])
                if label_info:
                    label = label_info[0].get("label", {}).get("name", "")

            results.append({
                "title": rec.get("title", ""),
                "artist_name": artist_name,
                "album_title": album_title,
                "musicbrainz_recording_id": rec.get("id", ""),
                "isrc": isrcs[0] if isrcs else "",
                "year": year,
                "label": label,
                "score": rec.get("score", 0),
                "duration_ms": rec.get("length", 0) or 0,
            })

        return results

    except Exception:
        logger.exception("musicbrainz_lookup_error")
        return []


async def lookup_album(title: str, artist: str = "") -> list[dict]:
    """Search MusicBrainz for an album (release)."""
    query_parts = []
    if title:
        query_parts.append(f'release:"{title}"')
    if artist:
        query_parts.append(f'artist:"{artist}"')

    query = " AND ".join(query_parts)
    if not query:
        return []

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{MB_API}/release",
                params={"query": query, "limit": 5, "fmt": "json"},
                headers={"User-Agent": MB_UA},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

        results = []
        for rel in data.get("releases", []):
            artist_credit = rel.get("artist-credit", [])
            artist_name = " ".join(ac.get("name", "") for ac in artist_credit) if artist_credit else ""

            label = ""
            label_info = rel.get("label-info", [])
            if label_info:
                label = label_info[0].get("label", {}).get("name", "")

            date = rel.get("date", "")
            year = None
            if date and len(date) >= 4:
                try:
                    year = int(date[:4])
                except ValueError:
                    pass

            results.append({
                "title": rel.get("title", ""),
                "artist_name": artist_name,
                "musicbrainz_release_id": rel.get("id", ""),
                "year": year,
                "label": label,
                "barcode": rel.get("barcode", ""),
                "country": rel.get("country", ""),
                "score": rel.get("score", 0),
                "track_count": rel.get("track-count", 0),
            })

        return results

    except Exception:
        logger.exception("musicbrainz_album_lookup_error")
        return []

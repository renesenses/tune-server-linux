"""Multi-source metadata enricher — MusicBrainz + Discogs + Last.fm."""

from __future__ import annotations

import aiohttp
import structlog

from tune_server.metadata_manager.matcher import lookup_track, lookup_album
from tune_server.metadata_manager.cover_fetcher import search_covers

logger = structlog.get_logger()

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"


async def get_lastfm_tags(
    title: str,
    artist: str,
    api_key: str,
) -> dict:
    """Get tags/genres and bio from Last.fm.

    Returns: {genres: [...], tags: [...], bio: str}
    """
    if not api_key or not title:
        return {}

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            params = {
                "method": "track.getInfo",
                "track": title,
                "artist": artist,
                "api_key": api_key,
                "format": "json",
            }
            async with session.get(LASTFM_API, params=params) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()

            track_info = data.get("track", {})
            tags = [t["name"] for t in track_info.get("toptags", {}).get("tag", [])]

            # Get artist bio
            bio = ""
            artist_info = track_info.get("artist", {})
            if artist_info:
                params2 = {
                    "method": "artist.getInfo",
                    "artist": artist,
                    "api_key": api_key,
                    "format": "json",
                }
                async with session.get(LASTFM_API, params=params2) as resp2:
                    if resp2.status == 200:
                        data2 = await resp2.json()
                        bio_info = data2.get("artist", {}).get("bio", {})
                        bio = bio_info.get("summary", "")
                        # Strip HTML
                        if "<" in bio:
                            import re
                            bio = re.sub(r"<[^>]+>", "", bio).strip()

            return {
                "tags": tags,
                "genres": tags[:3] if tags else [],
                "bio": bio,
            }

    except Exception:
        logger.debug("lastfm_error", title=title, artist=artist)
        return {}


async def enrich_track(
    track_id: int,
    title: str,
    artist: str,
    album: str = "",
    db=None,
    lastfm_key: str = "",
    discogs_token: str = "",
    cache_dir: str = "artwork_cache",
    auto_apply: bool = False,
    min_confidence: float = 0.9,
) -> dict:
    """Enrich a track from multiple sources.

    Merges data from MusicBrainz, Last.fm, and stores as suggestions
    (or auto-applies if confidence is high enough).

    Returns: {sources_queried: N, suggestions: N, auto_applied: N}
    """
    suggestions = []
    auto_applied = 0
    sources = 0

    # 1. MusicBrainz
    mb_results = await lookup_track(title, artist, album)
    sources += 1
    if mb_results:
        best = mb_results[0]
        confidence = best.get("score", 0) / 100.0

        # ISRC
        if best.get("isrc"):
            suggestions.append({
                "field": "isrc", "value": best["isrc"],
                "source": "musicbrainz", "confidence": confidence,
            })
        # MusicBrainz ID
        if best.get("musicbrainz_recording_id"):
            suggestions.append({
                "field": "musicbrainz_recording_id",
                "value": best["musicbrainz_recording_id"],
                "source": "musicbrainz", "confidence": confidence,
            })
        # Year
        if best.get("year"):
            suggestions.append({
                "field": "year", "value": str(best["year"]),
                "source": "musicbrainz", "confidence": confidence,
            })
        # Label
        if best.get("label"):
            suggestions.append({
                "field": "label", "value": best["label"],
                "source": "musicbrainz", "confidence": confidence,
            })

    # 2. Last.fm
    if lastfm_key:
        lfm = await get_lastfm_tags(title, artist, lastfm_key)
        sources += 1
        if lfm.get("genres"):
            suggestions.append({
                "field": "genre", "value": lfm["genres"][0],
                "source": "lastfm", "confidence": 0.7,
            })

    # Store suggestions in DB
    if db:
        for s in suggestions:
            if auto_apply and s["confidence"] >= min_confidence:
                # Auto-apply high confidence
                await db.execute(
                    f"UPDATE tracks SET {s['field']} = ? WHERE id = ?",
                    (s["value"], track_id),
                )
                await db.execute(
                    """INSERT INTO metadata_suggestions
                       (track_id, field, suggested_value, source, confidence, status)
                       VALUES (?, ?, ?, ?, ?, 'accepted')""",
                    (track_id, s["field"], s["value"], s["source"], s["confidence"]),
                )
                auto_applied += 1
            else:
                # Store as pending suggestion
                await db.execute(
                    """INSERT INTO metadata_suggestions
                       (track_id, field, suggested_value, source, confidence, status)
                       VALUES (?, ?, ?, ?, ?, 'pending')""",
                    (track_id, s["field"], s["value"], s["source"], s["confidence"]),
                )
        await db.commit()

    return {
        "track_id": track_id,
        "sources_queried": sources,
        "suggestions": len(suggestions),
        "auto_applied": auto_applied,
    }


async def enrich_album(
    album_id: int,
    title: str,
    artist: str = "",
    db=None,
    discogs_token: str = "",
    cache_dir: str = "artwork_cache",
) -> dict:
    """Enrich an album — MusicBrainz lookup + cover fetch."""
    suggestions = []

    # MusicBrainz album lookup
    mb_results = await lookup_album(title, artist)
    mb_release_id = ""
    if mb_results:
        best = mb_results[0]
        confidence = best.get("score", 0) / 100.0
        mb_release_id = best.get("musicbrainz_release_id", "")

        if mb_release_id:
            suggestions.append({
                "field": "musicbrainz_release_id", "value": mb_release_id,
                "source": "musicbrainz", "confidence": confidence,
            })
        if best.get("year"):
            suggestions.append({
                "field": "year", "value": str(best["year"]),
                "source": "musicbrainz", "confidence": confidence,
            })
        if best.get("label"):
            suggestions.append({
                "field": "label", "value": best["label"],
                "source": "musicbrainz", "confidence": confidence,
            })
        if best.get("barcode"):
            suggestions.append({
                "field": "barcode", "value": best["barcode"],
                "source": "musicbrainz", "confidence": confidence,
            })

    # Cover art
    covers = await search_covers(
        title, artist, mb_release_id, discogs_token, cache_dir)
    cover_path = covers[0]["local_path"] if covers else None

    # Store
    if db:
        for s in suggestions:
            await db.execute(
                """INSERT INTO metadata_suggestions
                   (album_id, field, suggested_value, source, confidence)
                   VALUES (?, ?, ?, ?, ?)""",
                (album_id, s["field"], s["value"], s["source"], s["confidence"]),
            )
        if cover_path:
            await db.execute(
                "UPDATE albums SET cover_path = ? WHERE id = ? AND (cover_path IS NULL OR cover_path = '')",
                (cover_path, album_id),
            )
        await db.commit()

    return {
        "album_id": album_id,
        "suggestions": len(suggestions),
        "cover_found": cover_path is not None,
        "cover_path": cover_path,
    }

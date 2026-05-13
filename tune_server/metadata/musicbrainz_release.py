"""MusicBrainz release lookup for album enrichment.

Queries the MusicBrainz API to find release IDs and release group IDs
for albums that are missing them from audio tags.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import aiohttp
import structlog

logger = structlog.get_logger()

MB_API = "https://musicbrainz.org/ws/2"
MB_UA = "TuneServer/0.7.77 (contact@mozaiklabs.fr)"
MB_RATE_LIMIT = 1.1  # slightly over 1s to stay safe


@dataclass
class MBReleaseMatch:
    """Result of a MusicBrainz release lookup."""
    release_id: str
    release_group_id: str | None
    title: str
    artist: str
    score: int  # MusicBrainz match score (0-100)


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation for fuzzy comparison."""
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


async def lookup_release(
    session: aiohttp.ClientSession,
    title: str,
    artist: str,
    track_count: int | None = None,
    year: int | None = None,
) -> MBReleaseMatch | None:
    """Query MusicBrainz for a release matching the given album metadata.

    Returns the best matching release or None if no good match is found.
    """
    # Build the Lucene query
    query_parts = [f'release:"{title}"', f'artist:"{artist}"']
    if track_count:
        query_parts.append(f"tracks:{track_count}")
    if year:
        query_parts.append(f"date:{year}")
    query = " AND ".join(query_parts)

    try:
        async with session.get(
            f"{MB_API}/release",
            params={"query": query, "limit": 5, "fmt": "json"},
            headers={"User-Agent": MB_UA},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.debug("mb_release_search_http_error", status=resp.status, title=title)
                return None
            data = await resp.json()
    except Exception:
        logger.debug("mb_release_search_error", title=title, artist=artist)
        return None

    releases = data.get("releases", [])
    if not releases:
        return None

    norm_title = _normalize(title)
    norm_artist = _normalize(artist)

    best: MBReleaseMatch | None = None
    best_score = 0

    for rel in releases:
        rel_title = rel.get("title", "")
        rel_score = rel.get("score", 0)

        # Check artist match
        artist_credit = rel.get("artist-credit", [])
        rel_artist = " ".join(
            ac.get("name", "") for ac in artist_credit
        ).strip() if artist_credit else ""

        # Title must be a reasonable match
        if _normalize(rel_title) != norm_title:
            # Allow partial match (e.g. "Kind of Blue" vs "Kind Of Blue")
            if norm_title not in _normalize(rel_title) and _normalize(rel_title) not in norm_title:
                continue

        # Artist should match at least partially
        if norm_artist and _normalize(rel_artist):
            if norm_artist not in _normalize(rel_artist) and _normalize(rel_artist) not in norm_artist:
                continue

        release_group = rel.get("release-group", {})
        release_group_id = release_group.get("id") if release_group else None

        match = MBReleaseMatch(
            release_id=rel["id"],
            release_group_id=release_group_id,
            title=rel_title,
            artist=rel_artist,
            score=rel_score,
        )

        if rel_score > best_score:
            best = match
            best_score = rel_score

    # Only return if score is decent (>= 80)
    if best and best.score >= 80:
        return best
    return None


async def enrich_album_musicbrainz_ids(
    album_id: int,
    album_repo,
) -> dict:
    """Enrich a single album with MusicBrainz release/release-group IDs.

    Returns dict with result details.
    """
    album = await album_repo.get(album_id)
    if not album:
        return {"enriched": False, "reason": "album_not_found"}

    if album.musicbrainz_release_id:
        return {"enriched": False, "reason": "already_has_release_id"}

    title = album.title
    artist = album.artist_name or ""
    if not title or not artist:
        return {"enriched": False, "reason": "missing_title_or_artist"}

    async with aiohttp.ClientSession() as session:
        match = await lookup_release(
            session,
            title=title,
            artist=artist,
            track_count=album.track_count or None,
            year=album.year,
        )

    if not match:
        return {"enriched": False, "reason": "no_match_found", "album_title": title, "artist": artist}

    await album_repo.update_musicbrainz_ids(
        album_id,
        release_id=match.release_id,
        release_group_id=match.release_group_id,
    )

    logger.info(
        "album_musicbrainz_enriched",
        album_id=album_id,
        title=title,
        release_id=match.release_id,
        release_group_id=match.release_group_id,
        score=match.score,
    )

    return {
        "enriched": True,
        "album_id": album_id,
        "album_title": title,
        "musicbrainz_release_id": match.release_id,
        "musicbrainz_release_group_id": match.release_group_id,
        "match_score": match.score,
    }


async def enrich_all_albums_musicbrainz_ids(album_repo) -> dict:
    """Enrich all albums missing MusicBrainz IDs.

    Rate-limited to 1 request/second. Returns summary stats.
    """
    albums = await album_repo.list_without_musicbrainz_ids()
    if not albums:
        logger.info("mb_enrich_all_no_albums_to_enrich")
        return {"enriched": 0, "total": 0, "errors": 0, "skipped": 0}

    enriched = 0
    errors = 0
    skipped = 0

    async with aiohttp.ClientSession() as session:
        for album in albums:
            title = album.title
            artist = album.artist_name or ""

            if not title or not artist:
                skipped += 1
                continue

            try:
                match = await lookup_release(
                    session,
                    title=title,
                    artist=artist,
                    track_count=album.track_count or None,
                    year=album.year,
                )

                if match:
                    await album_repo.update_musicbrainz_ids(
                        album.id,
                        release_id=match.release_id,
                        release_group_id=match.release_group_id,
                    )
                    enriched += 1
                    logger.debug(
                        "mb_album_enriched",
                        album_id=album.id,
                        title=title,
                        release_id=match.release_id,
                        score=match.score,
                    )
                else:
                    skipped += 1

            except Exception:
                logger.debug("mb_enrich_album_error", album_id=album.id, title=title)
                errors += 1

            # Rate limit: 1 request per second
            await asyncio.sleep(MB_RATE_LIMIT)

    logger.info(
        "mb_enrich_all_done",
        enriched=enriched,
        total=len(albums),
        errors=errors,
        skipped=skipped,
    )
    return {"enriched": enriched, "total": len(albums), "errors": errors, "skipped": skipped}

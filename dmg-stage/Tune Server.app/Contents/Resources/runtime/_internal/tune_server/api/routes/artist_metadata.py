"""Artist metadata enrichment routes — bio, similar artists, concerts via mozaiklabs.fr."""

from __future__ import annotations

import aiohttp
import structlog
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.exc import IntegrityError

from tune_server.api.deps import deps
from tune_server.config import settings

logger = structlog.get_logger()
router = APIRouter(prefix="/artists", tags=["artist-metadata"])


async def _resolve_mbid(artist_id: int) -> str:
    """Look up artist in DB and resolve MusicBrainz ID.

    If the artist has no mbid stored, attempt a MusicBrainz search by name
    and persist the result for future lookups.
    """
    artist = await deps.artist_repo.get(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")

    if artist.musicbrainz_id:
        return artist.musicbrainz_id

    # Try MusicBrainz API search by name
    mbid = await _search_musicbrainz_id(artist.name)
    if mbid:
        artist.musicbrainz_id = mbid
        try:
            await deps.artist_repo.update(artist)
            logger.info("artist_mbid_resolved", artist_id=artist_id, name=artist.name, mbid=mbid)
        except IntegrityError:
            # MBID already attached to another artist (e.g. duplicate-name
            # collision in MB search). Use the mbid for this request but
            # don't persist — the next call will re-search.
            logger.info(
                "artist_mbid_conflict",
                artist_id=artist_id, name=artist.name, mbid=mbid,
                hint="another artist already has this MBID; not persisting",
            )
        return mbid

    raise HTTPException(
        status_code=404,
        detail=f"No MusicBrainz ID found for artist '{artist.name}'",
    )


async def _search_musicbrainz_id(name: str) -> str | None:
    """Search MusicBrainz for an artist by name, return the top-match MBID."""
    url = "https://musicbrainz.org/ws/2/artist/"
    params = {"query": name, "fmt": "json", "limit": "1"}
    headers = {"User-Agent": "TuneServer/1.0 (https://mozaiklabs.fr)"}
    try:
        timeout = aiohttp.ClientTimeout(total=settings.api_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                artists = data.get("artists", [])
                if artists and artists[0].get("score", 0) >= 80:
                    return artists[0].get("id")
    except Exception:
        logger.exception("musicbrainz_search_error", name=name)
    return None


def _check_enabled() -> None:
    if not settings.artist_metadata_enabled:
        raise HTTPException(status_code=503, detail="Artist metadata enrichment is disabled")


@router.get("/{artist_id}/metadata")
async def get_artist_metadata(artist_id: int):
    """Get full enriched metadata for an artist."""
    _check_enabled()
    mbid = await _resolve_mbid(artist_id)
    data = await deps.artist_enrichment.get_artist(mbid)
    if data is None:
        raise HTTPException(status_code=404, detail="No enrichment data available")
    return data


@router.get("/{artist_id}/bio")
async def get_artist_bio(artist_id: int, lang: str = Query("fr")):
    """Get artist biography."""
    _check_enabled()
    mbid = await _resolve_mbid(artist_id)
    bio = await deps.artist_enrichment.get_bio(mbid, lang=lang)
    if bio is None:
        raise HTTPException(status_code=404, detail="No biography available")
    return {"mbid": mbid, "lang": lang, "bio": bio}


@router.get("/{artist_id}/similar")
async def get_similar_artists(artist_id: int):
    """Get similar artists."""
    _check_enabled()
    mbid = await _resolve_mbid(artist_id)
    similar = await deps.artist_enrichment.get_similar(mbid)
    return {"mbid": mbid, "artists": similar}


@router.get("/{artist_id}/concerts")
async def get_artist_concerts(artist_id: int):
    """Get upcoming concerts for an artist."""
    _check_enabled()
    mbid = await _resolve_mbid(artist_id)
    concerts = await deps.artist_enrichment.get_concerts(mbid)
    return {"mbid": mbid, "concerts": concerts}


@router.post("/{artist_id}/refresh")
async def refresh_artist_metadata(artist_id: int):
    """Force re-enrichment of artist metadata."""
    _check_enabled()
    mbid = await _resolve_mbid(artist_id)
    data = await deps.artist_enrichment.refresh(mbid)
    if data is None:
        raise HTTPException(status_code=502, detail="Enrichment refresh failed")
    return data

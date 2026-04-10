"""Podcast API routes — search, browse, episodes."""

from __future__ import annotations

from fastapi import APIRouter, Query

from tune_server.streaming.podcasts import PodcastService

router = APIRouter(prefix="/podcasts", tags=["podcasts"])

_service = PodcastService()


@router.get("/search")
async def search_podcasts(q: str = Query(..., min_length=1), limit: int = Query(20, le=50)):
    """Search podcasts via iTunes."""
    return await _service.search(q, limit=limit)


@router.get("/radiofrance")
async def radio_france_podcasts():
    """Curated list of Radio France podcasts."""
    return await _service.get_radio_france_podcasts()


@router.get("/episodes")
async def get_episodes(feed_url: str = Query(...), limit: int = Query(30, le=100)):
    """Fetch episodes from a podcast RSS feed."""
    return await _service.get_episodes(feed_url, limit=limit)

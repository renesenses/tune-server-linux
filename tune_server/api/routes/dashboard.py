"""Listening dashboard API routes — stats, tops, history charts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

import structlog

from fastapi import APIRouter, HTTPException, Query

from tune_server.api.deps import deps

logger = structlog.get_logger()

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class Period(str, Enum):
    week = "week"
    month = "month"
    year = "year"
    all = "all"


def _cutoff_for(period: Period) -> str | None:
    """Return an ISO-8601 cutoff timestamp string, or None for 'all'."""
    now = datetime.now(timezone.utc)
    if period == Period.week:
        dt = now - timedelta(days=7)
    elif period == Period.month:
        dt = now - timedelta(days=30)
    elif period == Period.year:
        dt = now - timedelta(days=365)
    else:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _ensure_db():
    if not deps.db:
        raise HTTPException(status_code=503, detail="Database not available")


# ---------------------------------------------------------------------------
# GET /dashboard/stats — aggregate counters
# ---------------------------------------------------------------------------

@router.get("/stats")
async def dashboard_stats():
    """Global listening statistics."""
    _ensure_db()
    row = await deps.db.fetchone(
        """SELECT
               COUNT(*) AS total_plays,
               COALESCE(SUM(listened_ms), 0) AS total_listened_ms,
               COUNT(DISTINCT track_id) AS unique_tracks,
               COUNT(DISTINCT album_title) AS unique_albums,
               COUNT(DISTINCT artist_name) AS unique_artists
           FROM playback_history"""
    )
    r = dict(row) if row else {}
    return {
        "total_plays": r.get("total_plays", 0),
        "total_listened_ms": r.get("total_listened_ms", 0),
        "total_listened_hours": round(r.get("total_listened_ms", 0) / 3_600_000, 1),
        "unique_tracks": r.get("unique_tracks", 0),
        "unique_albums": r.get("unique_albums", 0),
        "unique_artists": r.get("unique_artists", 0),
    }


# ---------------------------------------------------------------------------
# GET /dashboard/top-artists?period=month
# ---------------------------------------------------------------------------

@router.get("/top-artists")
async def top_artists(period: Period = Query(Period.month)):
    """Top 10 artists by play count for the given period."""
    _ensure_db()
    cutoff = _cutoff_for(period)
    if cutoff:
        rows = await deps.db.fetchall(
            """SELECT artist_name, COUNT(*) AS play_count,
                      COALESCE(SUM(listened_ms), 0) AS total_listened_ms,
                      MAX(played_at) AS last_played
               FROM playback_history
               WHERE artist_name IS NOT NULL AND artist_name != ''
                 AND played_at >= ?
               GROUP BY artist_name
               ORDER BY play_count DESC LIMIT 10""",
            (cutoff,),
        )
    else:
        rows = await deps.db.fetchall(
            """SELECT artist_name, COUNT(*) AS play_count,
                      COALESCE(SUM(listened_ms), 0) AS total_listened_ms,
                      MAX(played_at) AS last_played
               FROM playback_history
               WHERE artist_name IS NOT NULL AND artist_name != ''
               GROUP BY artist_name
               ORDER BY play_count DESC LIMIT 10"""
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /dashboard/top-albums?period=month
# ---------------------------------------------------------------------------

@router.get("/top-albums")
async def top_albums(period: Period = Query(Period.month)):
    """Top 10 albums by play count for the given period."""
    _ensure_db()
    cutoff = _cutoff_for(period)
    if cutoff:
        rows = await deps.db.fetchall(
            """SELECT ph.album_title, ph.artist_name,
                      COALESCE(ph.cover_path, al.cover_path) AS cover_path,
                      COUNT(*) AS play_count,
                      COALESCE(SUM(ph.listened_ms), 0) AS total_listened_ms,
                      MAX(ph.played_at) AS last_played
               FROM playback_history ph
               LEFT JOIN tracks t ON t.id = ph.track_id
               LEFT JOIN albums al ON al.id = t.album_id
               WHERE ph.album_title IS NOT NULL AND ph.album_title != ''
                 AND ph.played_at >= ?
               GROUP BY ph.album_title, ph.artist_name
               ORDER BY play_count DESC LIMIT 10""",
            (cutoff,),
        )
    else:
        rows = await deps.db.fetchall(
            """SELECT ph.album_title, ph.artist_name,
                      COALESCE(ph.cover_path, al.cover_path) AS cover_path,
                      COUNT(*) AS play_count,
                      COALESCE(SUM(ph.listened_ms), 0) AS total_listened_ms,
                      MAX(ph.played_at) AS last_played
               FROM playback_history ph
               LEFT JOIN tracks t ON t.id = ph.track_id
               LEFT JOIN albums al ON al.id = t.album_id
               WHERE ph.album_title IS NOT NULL AND ph.album_title != ''
               GROUP BY ph.album_title, ph.artist_name
               ORDER BY play_count DESC LIMIT 10"""
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /dashboard/top-tracks?period=month
# ---------------------------------------------------------------------------

@router.get("/top-tracks")
async def top_tracks(period: Period = Query(Period.month)):
    """Top 10 tracks by play count for the given period."""
    _ensure_db()
    cutoff = _cutoff_for(period)
    if cutoff:
        rows = await deps.db.fetchall(
            """SELECT ph.track_title, ph.artist_name, ph.album_title,
                      COALESCE(ph.cover_path, al.cover_path) AS cover_path,
                      COUNT(*) AS play_count,
                      COALESCE(SUM(ph.listened_ms), 0) AS total_listened_ms,
                      MAX(ph.played_at) AS last_played
               FROM playback_history ph
               LEFT JOIN tracks t ON t.id = ph.track_id
               LEFT JOIN albums al ON al.id = t.album_id
               WHERE ph.track_title IS NOT NULL AND ph.track_title != ''
                 AND ph.played_at >= ?
               GROUP BY ph.track_title, ph.artist_name, ph.album_title
               ORDER BY play_count DESC LIMIT 10""",
            (cutoff,),
        )
    else:
        rows = await deps.db.fetchall(
            """SELECT ph.track_title, ph.artist_name, ph.album_title,
                      COALESCE(ph.cover_path, al.cover_path) AS cover_path,
                      COUNT(*) AS play_count,
                      COALESCE(SUM(ph.listened_ms), 0) AS total_listened_ms,
                      MAX(ph.played_at) AS last_played
               FROM playback_history ph
               LEFT JOIN tracks t ON t.id = ph.track_id
               LEFT JOIN albums al ON al.id = t.album_id
               WHERE ph.track_title IS NOT NULL AND ph.track_title != ''
               GROUP BY ph.track_title, ph.artist_name, ph.album_title
               ORDER BY play_count DESC LIMIT 10"""
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /dashboard/listening-history?days=30
# ---------------------------------------------------------------------------

@router.get("/listening-history")
async def listening_history(days: int = Query(30, ge=1, le=365)):
    """Daily listening hours for the last N days (chart data)."""
    _ensure_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = await deps.db.fetchall(
        """SELECT DATE(played_at) AS day,
                  COUNT(*) AS play_count,
                  COALESCE(SUM(listened_ms), 0) AS total_listened_ms
           FROM playback_history
           WHERE played_at >= ?
           GROUP BY DATE(played_at)
           ORDER BY day""",
        (cutoff,),
    )
    return [
        {
            "day": r["day"],
            "play_count": r["play_count"],
            "total_listened_ms": r["total_listened_ms"],
            "hours": round(r["total_listened_ms"] / 3_600_000, 2),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# GET /dashboard/genre-breakdown?period=month
# ---------------------------------------------------------------------------

@router.get("/genre-breakdown")
async def genre_breakdown(period: Period = Query(Period.month)):
    """Genre distribution (pie chart data) based on play count.

    Joins playback_history -> tracks to get the genre tag.
    Tracks without a genre are grouped under "Unknown".
    """
    _ensure_db()
    cutoff = _cutoff_for(period)
    if cutoff:
        rows = await deps.db.fetchall(
            """SELECT COALESCE(t.genre, 'Unknown') AS genre,
                      COUNT(*) AS play_count,
                      COALESCE(SUM(ph.listened_ms), 0) AS total_listened_ms
               FROM playback_history ph
               LEFT JOIN tracks t ON t.id = ph.track_id
               WHERE ph.played_at >= ?
               GROUP BY COALESCE(t.genre, 'Unknown')
               ORDER BY play_count DESC""",
            (cutoff,),
        )
    else:
        rows = await deps.db.fetchall(
            """SELECT COALESCE(t.genre, 'Unknown') AS genre,
                      COUNT(*) AS play_count,
                      COALESCE(SUM(ph.listened_ms), 0) AS total_listened_ms
               FROM playback_history ph
               LEFT JOIN tracks t ON t.id = ph.track_id
               GROUP BY COALESCE(t.genre, 'Unknown')
               ORDER BY play_count DESC"""
        )
    return [dict(r) for r in rows]

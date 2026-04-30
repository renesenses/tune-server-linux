from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from tune_server.api.deps import deps
from tune_server.config import settings
from tune_server.db.repository import full_text_search
from tune_server.event_bus import Event, EventType
from tune_server.library.artwork import copy_cover_to_album_folder, fetch_cover_from_musicbrainz, get_album_artwork, save_artwork
from tune_server.library.metadata_reader import write_tags
from tune_server.models import (
    Album,
    AlbumUpdateRequest,
    Artist,
    ArtistUpdateRequest,
    BrowseDirectory,
    BrowseResult,
    BrowseRootEntry,
    BrowseRootsResponse,
    CompletenessStats,
    LibraryStatsResponse,
    SearchResult,
    Track,
    TrackCredit,
    TrackUpdateRequest,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/library", tags=["library"])


@router.get("/tracks", response_model=list[Track])
async def list_tracks(limit: int = Query(100, le=50000), offset: int = Query(0, ge=0)):
    return await deps.track_repo.list(limit=limit, offset=offset)


@router.get("/tracks/{track_id}", response_model=Track)
async def get_track(track_id: int):
    track = await deps.track_repo.get(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    return track


@router.get("/genre-tree")
async def get_genre_tree():
    """Return the user's genre tree (parent → list of subgenres).

    Hierarchical filtering uses this map without changing the flat-string
    `albums.genre` storage. A parent genre rule expands to itself + all
    direct children (Smart Playlists `branch_of` operator).
    """
    from tune_server.library import genre_tree
    return {"tree": genre_tree.load()}


@router.put("/genre-tree")
async def put_genre_tree(body: dict):
    """Replace the genre tree. Body: {"tree": {parent: [child, ...]}}.

    Validation is lenient — empty parents and non-string entries are
    silently dropped. Cache invalidates on save so the next read sees
    the new tree without server restart.
    """
    from tune_server.library import genre_tree
    tree = body.get("tree")
    if not isinstance(tree, dict):
        raise HTTPException(status_code=400, detail="Body must contain 'tree' (object)")
    genre_tree.save(tree)
    return {"ok": True, "tree": genre_tree.load()}


@router.get("/tracks/{track_id}/all-tags")
async def get_track_all_tags(track_id: int):
    """Return EVERY metadata field on a track: DB columns + every raw tag
    present in the audio file (mutagen) + audio info (sample_rate, etc.).

    Designed for the per-track edit drawer in the metadata view: a classical
    library can carry composer / conductor / performer / work / movement /
    opus / key / catalog tags that aren't in the canonical Track model.
    Returning the raw mutagen dict lets the UI render every tag actually
    used in this user's library, not a hardcoded subset.
    """
    from pathlib import Path

    track = await deps.track_repo.get(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    # 1) DB columns: all of them (Pydantic dump).
    try:
        db_fields = track.model_dump() if hasattr(track, "model_dump") else dict(track)
    except Exception:
        db_fields = {}

    # 2) Track credits.
    try:
        credits = await deps.credit_repo.list_by_track(track_id)
        db_credits = [
            (c.model_dump() if hasattr(c, "model_dump") else dict(c)) for c in credits
        ]
    except Exception:
        db_credits = []

    # 3) Raw audio file tags via mutagen + audio info.
    file_tags: dict[str, list[str]] = {}
    audio_info: dict = {}
    file_exists = False
    if track.file_path and Path(track.file_path).exists():
        file_exists = True
        try:
            import mutagen
            audio = mutagen.File(track.file_path)
            if audio is not None:
                # Stringify every tag value to a list of strings (handles
                # ID3 frames, MP4 atoms, Vorbis comments uniformly).
                if audio.tags:
                    for k in audio.tags.keys():
                        try:
                            v = audio.tags[k]
                            if hasattr(v, "text"):
                                vals = [str(x) for x in v.text]
                            elif isinstance(v, (list, tuple)):
                                vals = [str(x) for x in v]
                            else:
                                vals = [str(v)]
                        except Exception:
                            vals = ["<unreadable>"]
                        file_tags[str(k)] = vals
                info = getattr(audio, "info", None)
                if info is not None:
                    for attr in (
                        "length", "sample_rate", "bits_per_sample", "channels",
                        "bitrate", "codec", "codec_description", "version",
                        "layer", "mode", "protected", "encoder_info",
                    ):
                        if hasattr(info, attr):
                            try:
                                val = getattr(info, attr)
                                if val is None:
                                    continue
                                audio_info[attr] = val if isinstance(val, (int, float, str, bool)) else str(val)
                            except Exception:
                                pass
        except Exception as e:
            audio_info["_mutagen_error"] = str(e)[:300]

    return {
        "track_id": track_id,
        "file_path": track.file_path,
        "file_exists": file_exists,
        "db_fields": db_fields,
        "db_credits": db_credits,
        "file_tags": file_tags,
        "audio_info": audio_info,
    }



@router.post("/tracks/{track_id}/quick-fav")
async def quick_favorite_toggle(track_id: int, profile_id: int = 1):
    """Toggle favorite status for a track. Returns new state."""
    is_fav = await deps.db.fetchone(
        "SELECT 1 FROM user_favorites WHERE user_id = ? AND track_id = ?",
        (profile_id, track_id))

    if is_fav:
        await deps.db.execute(
            "DELETE FROM user_favorites WHERE user_id = ? AND track_id = ?",
            (profile_id, track_id))
        await deps.db.commit()
        return {"is_favorite": False, "track_id": track_id}
    else:
        await deps.db.execute(
            "INSERT INTO user_favorites (user_id, track_id) VALUES (?, ?)",
            (profile_id, track_id))
        await deps.db.commit()
        return {"is_favorite": True, "track_id": track_id}


@router.get("/tracks/{track_id}/credits", response_model=list[TrackCredit])
async def get_track_credits(track_id: int):
    track = await deps.track_repo.get(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if not deps.credit_repo:
        return []
    return await deps.credit_repo.list_by_track(track_id)


@router.get("/tracks/{track_id}/audio")
async def stream_track_audio(track_id: int):
    from tune_server.api.deps import deps
    track = await deps.track_repo.get(track_id)
    if not track or not track.file_path:
        raise HTTPException(status_code=404, detail="Track not found")
    filepath = Path(track.file_path)
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    suffix = filepath.suffix.lower()
    mt = {".flac": "audio/flac", ".mp3": "audio/mpeg", ".wav": "audio/wav",
          ".aac": "audio/aac", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
          ".opus": "audio/opus", ".aiff": "audio/aiff", ".dsf": "audio/dsf"}
    return FileResponse(filepath, media_type=mt.get(suffix, "application/octet-stream"), filename=filepath.name)

@router.post("/albums/{album_id}/quick-fav")
async def quick_favorite_album(album_id: int, profile_id: int = 1):
    """Toggle favorite status for an album. Returns new state."""
    is_fav = await deps.db.fetchone(
        "SELECT 1 FROM user_favorites WHERE user_id = ? AND album_id = ?",
        (profile_id, album_id))
    if is_fav:
        await deps.db.execute(
            "DELETE FROM user_favorites WHERE user_id = ? AND album_id = ?",
            (profile_id, album_id))
        await deps.db.commit()
        return {"is_favorite": False, "album_id": album_id}
    else:
        await deps.db.execute(
            "INSERT INTO user_favorites (user_id, album_id) VALUES (?, ?)",
            (profile_id, album_id))
        await deps.db.commit()
        return {"is_favorite": True, "album_id": album_id}


@router.get("/albums/top-rated")
async def top_rated_albums(limit: int = 20):
    if not deps.album_rating_repo:
        return []
    return await deps.album_rating_repo.top_rated(limit)


@router.get("/albums/recent", response_model=list[Album])
async def list_recent_albums(limit: int = Query(50, le=200)):
    """Albums recently added to the library, sorted by creation date descending."""
    return await deps.album_repo.list_recent(limit=limit)


@router.get("/albums")
async def list_albums(
    limit: int = Query(100, le=50000),
    offset: int = Query(0, ge=0),
    quality: str | None = Query(None, description="Filter by quality: hi-res, cd, lossy, dsd"),
    format: str | None = Query(None, description="Filter by format: flac, mp3, aac, wav, dsd"),
    sample_rate: int | None = Query(None, description="Filter by min sample rate in Hz (e.g. 96000)"),
):
    albums = await deps.album_repo.list(
        limit=limit, offset=offset, quality=quality,
        format=format, sample_rate=sample_rate,
    )
    result = [a.model_dump(exclude_none=False) for a in albums]

    # Add folder_path from first track of each album
    album_ids = [a.id for a in albums if a.id]
    if album_ids:
        placeholders = ", ".join(["?" for _ in album_ids])
        paths = await deps.db.fetchall(
            f"""SELECT album_id, MIN(file_path) as file_path
                FROM tracks
                WHERE album_id IN ({placeholders}) AND file_path IS NOT NULL
                GROUP BY album_id""",
            tuple(album_ids),
        )
        path_map = {}
        for r in paths:
            fp = r["file_path"]
            if fp:
                # Remove filename to get folder
                idx = fp.rfind("/")
                path_map[r["album_id"]] = fp[:idx] if idx > 0 else fp
        for item in result:
            item["folder_path"] = path_map.get(item.get("id"))

    return result


@router.get("/albums/filters")
async def get_album_filters():
    """Return available format and sample rate values for filtering."""
    rows = await deps.db.fetchall("""
        SELECT DISTINCT format, sample_rate FROM tracks
        WHERE format IS NOT NULL AND sample_rate IS NOT NULL
        ORDER BY format, sample_rate
    """)
    formats = sorted({r["format"] for r in rows if r["format"]})
    sample_rates = sorted({r["sample_rate"] for r in rows if r["sample_rate"]})
    return {"formats": formats, "sample_rates": sample_rates}


@router.get("/albums/{album_id}", response_model=Album)
async def get_album(album_id: int):
    album = await deps.album_repo.get(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    return album


@router.get("/albums/{album_id}/tracks", response_model=list[Track])
async def get_album_tracks(album_id: int):
    return await deps.track_repo.list_by_album(album_id)


@router.post("/albums/{album_id}/rate")
async def rate_album(album_id: int, body: dict):
    rating = body.get("rating")
    note = body.get("note")
    profile_id = body.get("profile_id")
    if not rating or rating < 1 or rating > 5:
        raise HTTPException(400, "Rating must be 1-5")
    if not deps.album_rating_repo:
        raise HTTPException(503, "Rating not available")
    result = await deps.album_rating_repo.rate(album_id, rating, note, profile_id)
    return result


@router.get("/albums/{album_id}/rating")
async def get_album_rating(album_id: int, profile_id: int | None = None):
    if not deps.album_rating_repo:
        return {"album_id": album_id, "rating": None, "note": None}
    result = await deps.album_rating_repo.get(album_id, profile_id)
    return result or {"album_id": album_id, "rating": None, "note": None}


@router.get("/artists", response_model=list[Artist])
async def list_artists(
    limit: int = Query(100, le=5000),
    offset: int = Query(0, ge=0),
    include_credits_only: bool = Query(
        False,
        description="Include credit-only artists (composers/performers populated from track tags but with no own album/track).",
    ),
):
    return await deps.artist_repo.list(
        limit=limit, offset=offset,
        principal_only=not include_credits_only,
    )


@router.post("/artists", response_model=Artist, status_code=201)
async def create_artist(req: ArtistUpdateRequest):
    if not req.name:
        raise HTTPException(status_code=400, detail="Name is required")
    artist = Artist(name=req.name, sort_name=req.sort_name, bio=req.bio)
    artist_id = await deps.artist_repo.create(artist)
    return await deps.artist_repo.get(artist_id)


@router.get("/artists/{artist_id}", response_model=Artist)
async def get_artist(artist_id: int):
    artist = await deps.artist_repo.get(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    return artist


@router.get("/artists/{artist_id}/albums", response_model=list[Album])
async def get_artist_albums(artist_id: int):
    return await deps.album_repo.list_by_artist(artist_id)


@router.get("/artists/{artist_id}/tracks", response_model=list[Track])
async def get_artist_tracks(artist_id: int):
    return await deps.track_repo.list_by_artist(artist_id)


@router.get("/artists/{artist_id}/credits", response_model=list[TrackCredit])
async def get_artist_credits(artist_id: int):
    artist = await deps.artist_repo.get(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    if not deps.credit_repo:
        return []
    return await deps.credit_repo.list_by_artist(artist_id)


@router.post("/tracks/{track_id}/credits/enrich")
async def enrich_track_credits_endpoint(track_id: int):
    """Enrich a track's credits with instruments from MusicBrainz."""
    if not deps.credit_repo or not deps.track_repo:
        raise HTTPException(status_code=503, detail="Credits not available")
    from tune_server.metadata_manager.credit_enricher import enrich_track_credits
    result = await enrich_track_credits(track_id, deps.track_repo, deps.credit_repo, deps.album_repo)
    return result


@router.post("/albums/{album_id}/credits/enrich")
async def enrich_album_credits_endpoint(album_id: int):
    """Enrich credits for an album — looks up each credited artist on MusicBrainz for their instrument."""
    if not deps.credit_repo or not deps.track_repo:
        raise HTTPException(status_code=503, detail="Credits not available")
    from tune_server.metadata_manager.credit_enricher import enrich_credits_instruments
    result = await enrich_credits_instruments(deps.credit_repo, album_id=album_id, track_repo=deps.track_repo)
    return result


@router.post("/enrich-credits")
async def enrich_all_credits_endpoint():
    """Enrich ALL credits without instruments from MusicBrainz (background task)."""
    if not deps.credit_repo:
        raise HTTPException(status_code=503, detail="Credits not available")
    from tune_server.metadata_manager.credit_enricher import enrich_credits_instruments

    async def _run():
        result = await enrich_credits_instruments(deps.credit_repo)
        logger.info("enrich_all_credits_done", **result)

    asyncio.create_task(_run())
    return {"status": "started"}


@router.get("/search", response_model=SearchResult)
async def search(q: str = Query(..., min_length=1), limit: int = Query(50, le=200)):
    return await full_text_search(deps.db, q, limit=limit)


@router.get("/stats", response_model=LibraryStatsResponse)
async def library_stats():
    return LibraryStatsResponse(
        tracks=await deps.track_repo.count(),
        albums=await deps.album_repo.count(),
        artists=await deps.artist_repo.count(),
    )


@router.get("/history")
async def playback_history(limit: int = Query(50, le=200)):
    """Recently played tracks."""
    if not deps.history_repo:
        return []
    return await deps.history_repo.list_recent(limit)


@router.get("/history/top-tracks")
async def top_tracks(limit: int = Query(20, le=100)):
    """Most played tracks."""
    if not deps.history_repo:
        return []
    return await deps.history_repo.top_tracks(limit)


@router.get("/history/top-artists")
async def top_artists(limit: int = Query(20, le=100)):
    """Most played artists."""
    if not deps.history_repo:
        return []
    return await deps.history_repo.top_artists(limit)


_DASHBOARD_CACHE: dict[tuple, tuple[float, dict]] = {}


def _cache_ttl_for(period: str) -> int:
    # Today changes by the minute; older periods barely move from one
    # request to the next. Cache aggressively to keep the dashboard
    # cheap for big-library setups.
    return {"today": 60, "7d": 300, "30d": 300, "all": 3600}.get(period, 300)


def _cutoff_for(period: str, now: datetime) -> datetime | None:
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "7d":
        return now - timedelta(days=7)
    if period == "30d":
        return now - timedelta(days=30)
    return None  # "all" — no cutoff


@router.get("/history/dashboard")
async def history_dashboard(
    period: str = "30d",
    zone_id: int | None = None,
    profile_id: int | None = None,
    top_n: int = 10,
):
    """Listening dashboard — totals, top artists/albums/tracks, trend,
    hourly, by-zone, by-source, completion ratio. All scoped by period
    (today / 7d / 30d / all) and optionally filtered to a zone or
    profile.
    """
    if period not in ("today", "7d", "30d", "all"):
        period = "30d"
    top_n = max(1, min(top_n, 50))

    cache_key = (period, zone_id, profile_id, top_n)
    now_mono = time.monotonic()
    cached = _DASHBOARD_CACHE.get(cache_key)
    if cached and now_mono - cached[0] < _cache_ttl_for(period):
        return cached[1]

    if not deps.history_repo:
        empty = {
            "period": period, "totals": {"plays": 0, "listening_ms": 0,
                "unique_tracks": 0, "unique_artists": 0},
            "top_artists": [], "top_albums": [], "top_tracks": [],
            "trend": [], "hourly": [], "by_zone": [], "by_source": [],
            "completion": {"completed": 0, "skipped": 0,
                "avg_listened_ms": 0, "avg_track_duration_ms": 0},
        }
        return empty

    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    cutoff = _cutoff_for(period, now)
    is_postgres = settings.db_engine == "postgres"
    hour_expr = "EXTRACT(HOUR FROM played_at)::INTEGER" if is_postgres \
        else "CAST(strftime('%H', played_at) AS INTEGER)"

    # Build the shared WHERE clause once. Each query appends its own
    # GROUP BY / ORDER BY but reuses these conditions and parameters so
    # the composite indexes (user_id, played_at) etc. all match.
    where_parts: list[str] = []
    params: list = []
    if cutoff is not None:
        where_parts.append("played_at > ?")
        params.append(cutoff)
    if zone_id is not None:
        where_parts.append("zone_id = ?")
        params.append(zone_id)
    if profile_id is not None:
        where_parts.append("(user_id IS NULL OR user_id = ?)")
        params.append(profile_id)
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    async def q_totals():
        return await deps.db.fetchone(
            f"""SELECT COUNT(*) as plays,
                       COALESCE(SUM(listened_ms), 0) as total_ms,
                       COUNT(DISTINCT track_id) as unique_tracks,
                       COUNT(DISTINCT artist_name) as unique_artists
                FROM playback_history {where_clause}""",
            tuple(params),
        )

    async def q_top_artists():
        return await deps.db.fetchall(
            f"""SELECT artist_name, COUNT(*) as plays,
                       COALESCE(SUM(listened_ms), 0) as listening_ms
                FROM playback_history {where_clause}
                  {'AND' if where_parts else 'WHERE'} artist_name IS NOT NULL
                GROUP BY artist_name
                ORDER BY plays DESC
                LIMIT ?""",
            tuple(params) + (top_n,),
        )

    async def q_top_albums():
        return await deps.db.fetchall(
            f"""SELECT album_title, artist_name,
                       MAX(cover_path) as cover_path,
                       COUNT(*) as plays
                FROM playback_history {where_clause}
                  {'AND' if where_parts else 'WHERE'} album_title IS NOT NULL
                GROUP BY album_title, artist_name
                ORDER BY plays DESC
                LIMIT ?""",
            tuple(params) + (top_n,),
        )

    async def q_top_tracks():
        return await deps.db.fetchall(
            f"""SELECT track_id, track_title, artist_name,
                       COUNT(*) as plays,
                       COALESCE(SUM(listened_ms), 0) as listening_ms
                FROM playback_history {where_clause}
                  {'AND' if where_parts else 'WHERE'} track_title IS NOT NULL
                GROUP BY track_id, track_title, artist_name
                ORDER BY plays DESC
                LIMIT ?""",
            tuple(params) + (top_n,),
        )

    async def q_trend():
        return await deps.db.fetchall(
            f"""SELECT DATE(played_at) as day, COUNT(*) as plays,
                       COALESCE(SUM(listened_ms), 0) as ms
                FROM playback_history {where_clause}
                GROUP BY DATE(played_at)
                ORDER BY day""",
            tuple(params),
        )

    async def q_hourly():
        return await deps.db.fetchall(
            f"""SELECT {hour_expr} as hour, COUNT(*) as plays
                FROM playback_history {where_clause}
                GROUP BY hour
                ORDER BY hour""",
            tuple(params),
        )

    async def q_by_zone():
        return await deps.db.fetchall(
            f"""SELECT zone_id, COUNT(*) as plays,
                       COALESCE(SUM(listened_ms), 0) as listening_ms
                FROM playback_history {where_clause}
                GROUP BY zone_id
                ORDER BY plays DESC""",
            tuple(params),
        )

    async def q_by_source():
        return await deps.db.fetchall(
            f"""SELECT source, COUNT(*) as plays,
                       COALESCE(SUM(listened_ms), 0) as listening_ms
                FROM playback_history {where_clause}
                GROUP BY source
                ORDER BY plays DESC""",
            tuple(params),
        )

    async def q_by_genre():
        # Genre is on `albums`, not on playback_history. We try two paths:
        # 1) ph.track_id → tracks.album_id → albums  (local, freshest)
        # 2) (ph.artist_name, ph.album_title) → albums  (streaming or rows
        #    where track_id wasn't populated at write time — they still
        #    carry artist_name + album_title denormalized).
        # Aliased as `genre_label` to avoid the ambiguous-column error PG
        # throws when GROUP BY reads the SELECT alias and a joined table
        # has a column called `genre`.
        return await deps.db.fetchall(
            f"""SELECT COALESCE(
                          NULLIF(al_by_track.genre, ''),
                          NULLIF(al_by_meta.genre, ''),
                          '—'
                       ) as genre_label,
                       COUNT(*) as plays,
                       COALESCE(SUM(ph.listened_ms), 0) as listening_ms
                FROM playback_history ph
                LEFT JOIN tracks t ON t.id = ph.track_id
                LEFT JOIN albums al_by_track ON al_by_track.id = t.album_id
                LEFT JOIN artists ar ON LOWER(ar.name) = LOWER(ph.artist_name)
                LEFT JOIN albums al_by_meta
                    ON al_by_track.id IS NULL
                   AND LOWER(al_by_meta.title) = LOWER(ph.album_title)
                   AND al_by_meta.artist_id = ar.id
                {where_clause.replace('played_at', 'ph.played_at').replace('zone_id', 'ph.zone_id').replace('user_id', 'ph.user_id')}
                GROUP BY genre_label
                ORDER BY plays DESC""",
            tuple(params),
        )

    async def q_weekday_hourly():
        # 7×24 grid: rows = day-of-week (PG: 0=Sunday … 6=Saturday;
        # SQLite strftime('%w'): 0=Sunday … 6=Saturday too — they agree).
        # We re-map to ISO 8601 1=Monday … 7=Sunday at projection time
        # so the UI shows weeks starting on Monday (FR convention).
        if is_postgres:
            return await deps.db.fetchall(
                f"""SELECT EXTRACT(DOW FROM played_at)::INTEGER as raw_dow,
                           EXTRACT(HOUR FROM played_at)::INTEGER as hour,
                           COUNT(*) as plays
                    FROM playback_history {where_clause}
                    GROUP BY raw_dow, hour
                    ORDER BY raw_dow, hour""",
                tuple(params),
            )
        return await deps.db.fetchall(
            f"""SELECT CAST(strftime('%w', played_at) AS INTEGER) as raw_dow,
                       CAST(strftime('%H', played_at) AS INTEGER) as hour,
                       COUNT(*) as plays
                FROM playback_history {where_clause}
                GROUP BY raw_dow, hour
                ORDER BY raw_dow, hour""",
            tuple(params),
        )

    async def q_streak():
        # Longest current consecutive-day streak (counting back from today)
        # + longest historical streak overall.
        date_expr = "DATE(ph.played_at)" if not is_postgres else "DATE(ph.played_at)"
        rows = await deps.db.fetchall(
            f"""SELECT DISTINCT {date_expr} as d
                FROM playback_history ph
                ORDER BY d DESC""",
        )
        if not rows:
            return {"current": 0, "best": 0, "last_day": None}
        # Convert to a sorted set of date strings for portability.
        from datetime import date as _date, timedelta as _td
        days = []
        for r in rows:
            v = r["d"]
            if isinstance(v, str):
                try: v = _date.fromisoformat(v[:10])
                except Exception: continue
            days.append(v)
        days = sorted(set(days), reverse=True)
        if not days:
            return {"current": 0, "best": 0, "last_day": None}
        today = _date.today()
        # Current streak: from today (or yesterday — listening today not
        # required) walk backward as long as days are consecutive.
        current = 0
        cursor = today
        if days[0] == today:
            current = 1
            cursor = today - _td(days=1)
            for d in days[1:]:
                if d == cursor:
                    current += 1
                    cursor -= _td(days=1)
                else:
                    break
        elif days[0] == today - _td(days=1):
            # No play today yet — "yesterday-anchored" streak still counts.
            current = 1
            cursor = today - _td(days=2)
            for d in days[1:]:
                if d == cursor:
                    current += 1
                    cursor -= _td(days=1)
                else:
                    break
        # Best historical streak.
        best = 1
        run = 1
        for i in range(1, len(days)):
            if days[i] == days[i-1] - _td(days=1):
                run += 1
                if run > best: best = run
            else:
                run = 1
        return {
            "current": current,
            "best": best,
            "last_day": days[0].isoformat(),
        }

    async def q_on_this_day():
        # Tracks played on this calendar day in past years.
        if is_postgres:
            sql = """SELECT track_title, artist_name, album_title, cover_path,
                            played_at, EXTRACT(YEAR FROM played_at)::INTEGER as year
                     FROM playback_history
                     WHERE EXTRACT(MONTH FROM played_at) = EXTRACT(MONTH FROM CURRENT_DATE)
                       AND EXTRACT(DAY   FROM played_at) = EXTRACT(DAY   FROM CURRENT_DATE)
                       AND DATE(played_at) < CURRENT_DATE
                     ORDER BY played_at DESC
                     LIMIT 30"""
        else:
            sql = """SELECT track_title, artist_name, album_title, cover_path,
                            played_at,
                            CAST(strftime('%Y', played_at) AS INTEGER) as year
                     FROM playback_history
                     WHERE strftime('%m-%d', played_at) = strftime('%m-%d', 'now')
                       AND DATE(played_at) < DATE('now')
                     ORDER BY played_at DESC
                     LIMIT 30"""
        return await deps.db.fetchall(sql)

    async def q_completion():
        # Completed = listened_ms >= 0.85 * duration_ms. Both NULL/0
        # fields fall through to "skipped" so the bucket totals add up
        # to the row count.
        return await deps.db.fetchone(
            f"""SELECT
                  SUM(CASE WHEN duration_ms > 0 AND listened_ms >= duration_ms * 0.85 THEN 1 ELSE 0 END) as completed,
                  SUM(CASE WHEN duration_ms > 0 AND listened_ms < duration_ms * 0.85 THEN 1 ELSE 0 END) as skipped,
                  COALESCE(AVG(listened_ms), 0) as avg_listened_ms,
                  COALESCE(AVG(duration_ms), 0) as avg_track_duration_ms
                FROM playback_history {where_clause}""",
            tuple(params),
        )

    # Fan out — total wall time = max query, not sum.
    (totals_row, top_artists, top_albums, top_tracks, trend, hourly,
     by_zone, by_source, by_genre, weekday_hourly, streak, on_this_day,
     completion_row) = await asyncio.gather(
        q_totals(), q_top_artists(), q_top_albums(), q_top_tracks(),
        q_trend(), q_hourly(), q_by_zone(), q_by_source(), q_by_genre(),
        q_weekday_hourly(), q_streak(), q_on_this_day(),
        q_completion(),
    )

    # Zone names — cheap lookup once.
    zone_names: dict[int, str] = {}
    if deps.zone_manager and by_zone:
        try:
            for z in await deps.zone_manager.list_zones():
                zone_names[z.id] = z.name
        except Exception:
            pass

    range_from = cutoff.isoformat() if cutoff else None

    response = {
        "period": period,
        "range": {"from": range_from, "to": now.isoformat()},
        "totals": {
            "plays": totals_row["plays"] if totals_row else 0,
            "listening_ms": totals_row["total_ms"] if totals_row else 0,
            "unique_tracks": totals_row["unique_tracks"] if totals_row else 0,
            "unique_artists": totals_row["unique_artists"] if totals_row else 0,
        },
        "top_artists": [
            {"artist_name": r["artist_name"], "plays": r["plays"],
             "listening_ms": r["listening_ms"]} for r in top_artists],
        "top_albums": [
            {"album_title": r["album_title"], "artist_name": r["artist_name"],
             "cover_path": r["cover_path"], "plays": r["plays"]} for r in top_albums],
        "top_tracks": [
            {"track_id": r["track_id"], "title": r["track_title"],
             "artist_name": r["artist_name"], "plays": r["plays"],
             "listening_ms": r["listening_ms"]} for r in top_tracks],
        "trend": [{"day": r["day"], "plays": r["plays"],
                   "listening_ms": r["ms"]} for r in trend],
        "hourly": [{"hour": r["hour"], "plays": r["plays"]} for r in hourly],
        "by_zone": [
            {"zone_id": r["zone_id"],
             "zone_name": zone_names.get(r["zone_id"]),
             "plays": r["plays"], "listening_ms": r["listening_ms"]}
            for r in by_zone],
        "by_source": [
            {"source": r["source"], "plays": r["plays"],
             "listening_ms": r["listening_ms"]} for r in by_source],
        "by_genre": [
            {"genre": r["genre_label"], "plays": r["plays"],
             "listening_ms": r["listening_ms"]} for r in by_genre],
        "weekday_hourly": [
            # Re-map raw 0=Sunday … 6=Saturday into ISO 1=Monday … 7=Sunday
            # so the UI can render weeks starting on Monday without extra
            # arithmetic.
            {
                "weekday": ((int(r["raw_dow"]) + 6) % 7) + 1,
                "hour": int(r["hour"]),
                "plays": int(r["plays"]),
            } for r in weekday_hourly
        ],
        "streak": streak,
        "on_this_day": [
            {
                "track_title": r["track_title"],
                "artist_name": r["artist_name"],
                "album_title": r["album_title"],
                "cover_path": r["cover_path"],
                "played_at": str(r["played_at"]) if r["played_at"] else None,
                "year": int(r["year"]) if r.get("year") else None,
            } for r in on_this_day],
        "completion": {
            "completed": int(completion_row["completed"] or 0) if completion_row else 0,
            "skipped": int(completion_row["skipped"] or 0) if completion_row else 0,
            "avg_listened_ms": int(completion_row["avg_listened_ms"] or 0) if completion_row else 0,
            "avg_track_duration_ms": int(completion_row["avg_track_duration_ms"] or 0) if completion_row else 0,
        },
    }
    _DASHBOARD_CACHE[cache_key] = (now_mono, response)
    return response


@router.get("/recommendations")
async def get_recommendations(limit: int = Query(20, le=100)):
    """Get album recommendations based on listening history."""
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    cutoff_30d = now - timedelta(days=30)
    cutoff_7d = now - timedelta(days=7)

    # Get top artists and genres from history (last 30 days)
    history_rows = await deps.db.fetchall(
        """SELECT ph.artist_name, a.genre, COUNT(*) as cnt
           FROM playback_history ph
           LEFT JOIN tracks t ON ph.track_id = t.id
           LEFT JOIN albums a ON t.album_id = a.id
           WHERE ph.played_at > ?
           GROUP BY ph.artist_name, a.genre
           ORDER BY cnt DESC
           LIMIT 20""",
        (cutoff_30d,),
    )

    if not history_rows:
        # Fallback: random albums
        albums = await deps.album_repo.list(limit=limit)
        return {
            "recommendations": [a.model_dump(exclude_none=False) for a in albums],
            "reason": "random",
        }

    # Collect top genres and artists
    top_genres = set()
    top_artists = set()
    for row in history_rows:
        if row["genre"]:
            top_genres.add(row["genre"])
        if row["artist_name"]:
            top_artists.add(row["artist_name"])

    # Find albums in those genres/by those artists that user hasn't listened to recently
    recommendations = []

    # By genre
    for genre in list(top_genres)[:5]:
        genre_albums = await deps.db.fetchall(
            """SELECT a.id, a.title, a.artist_name, a.year, a.genre,
                      a.cover_path, a.format, a.sample_rate, a.bit_depth
               FROM albums a
               WHERE a.genre LIKE ?
               AND NOT EXISTS (
                   SELECT 1 FROM playback_history ph2
                   JOIN tracks t2 ON ph2.track_id = t2.id
                   WHERE t2.album_id = a.id AND ph2.played_at > ?
               )
               ORDER BY RANDOM() LIMIT 3""",
            (f"%{genre}%", cutoff_7d),
        )
        for r in genre_albums:
            recommendations.append({
                "id": r["id"], "title": r["title"], "artist_name": r["artist_name"],
                "year": r["year"], "genre": r["genre"], "cover_path": r["cover_path"],
                "format": r["format"], "sample_rate": r["sample_rate"],
                "bit_depth": r["bit_depth"], "reason": f"Genre: {genre}",
            })

    # By artist (other albums)
    for artist in list(top_artists)[:5]:
        artist_albums = await deps.db.fetchall(
            """SELECT a.id, a.title, a.artist_name, a.year, a.genre,
                      a.cover_path, a.format, a.sample_rate, a.bit_depth
               FROM albums a
               WHERE a.artist_name = ?
               AND NOT EXISTS (
                   SELECT 1 FROM playback_history ph2
                   JOIN tracks t2 ON ph2.track_id = t2.id
                   WHERE t2.album_id = a.id AND ph2.played_at > ?
               )
               ORDER BY RANDOM() LIMIT 2""",
            (artist, cutoff_7d),
        )
        for r in artist_albums:
            recommendations.append({
                "id": r["id"], "title": r["title"], "artist_name": r["artist_name"],
                "year": r["year"], "genre": r["genre"], "cover_path": r["cover_path"],
                "format": r["format"], "sample_rate": r["sample_rate"],
                "bit_depth": r["bit_depth"], "reason": f"Artiste: {artist}",
            })

    # Deduplicate and limit
    seen = set()
    unique = []
    for r in recommendations:
        if r["id"] not in seen:
            seen.add(r["id"])
            unique.append(r)

    return {"recommendations": unique[:limit]}


# --- Smart Playlists ---

@router.get("/tracks/{track_id}/lyrics")
async def get_track_lyrics(track_id: int):
    """Get lyrics for a track. Tries DB first, then file tags, then online."""
    track = await deps.track_repo.get(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    # 1. Check DB (lyrics column may not be in Track model)
    try:
        row = await deps.db.fetchone("SELECT lyrics FROM tracks WHERE id = ?", (track_id,))
        if row and row["lyrics"]:
            return {"lyrics": row["lyrics"], "source": "database"}
    except Exception:
        pass

    # 2. Try file tags
    if track.file_path and not track.file_path.startswith("http"):
        try:
            from mutagen import File as MutagenFile
            from pathlib import Path
            if Path(track.file_path).exists():
                audio = MutagenFile(track.file_path)
                if audio and audio.tags:
                    lyrics_text = None
                    # FLAC/Vorbis
                    for key in ("lyrics", "LYRICS", "UNSYNCEDLYRICS"):
                        val = audio.tags.get(key)
                        if val:
                            lyrics_text = str(val[0]) if isinstance(val, list) else str(val)
                            break
                    # ID3 (MP3)
                    if not lyrics_text:
                        for key in audio.tags:
                            if key.startswith("USLT"):
                                lyrics_text = str(audio.tags[key])
                                break
                    # MP4
                    if not lyrics_text:
                        val = audio.tags.get("\xa9lyr")
                        if val:
                            lyrics_text = str(val[0]) if isinstance(val, list) else str(val)

                    if lyrics_text and lyrics_text.strip():
                        # Save to DB for next time
                        await deps.db.execute(
                            "UPDATE tracks SET lyrics = ? WHERE id = ?",
                            (lyrics_text.strip(), track_id),
                        )
                        await deps.db.commit()
                        return {"lyrics": lyrics_text.strip(), "source": "tags"}
        except Exception:
            pass

    # 3. Try mozaiklabs.fr shared cache
    import aiohttp
    title = track.title
    artist = track.artist_name or ""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://mozaiklabs.fr/api/v1/lyrics",
                params={"title": title, "artist": artist},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("lyrics"):
                        # Cache locally
                        await deps.db.execute(
                            "UPDATE tracks SET lyrics = ? WHERE id = ?",
                            (data["lyrics"], track_id),
                        )
                        await deps.db.commit()
                        return {"lyrics": data["lyrics"], "source": "mozaiklabs"}
    except Exception:
        pass

    # 4. Try lrclib.net (free, no API key)
    try:
        params = {"track_name": title, "artist_name": artist}
        if track.album_title:
            params["album_name"] = track.album_title

        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://lrclib.net/api/get",
                params=params,
                headers={"User-Agent": "TuneServer/0.8.0"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    synced = data.get("syncedLyrics") or ""
                    lyrics_text = data.get("plainLyrics") or synced
                    if lyrics_text and lyrics_text.strip():
                        # Cache locally
                        await deps.db.execute(
                            "UPDATE tracks SET lyrics = ? WHERE id = ?",
                            (lyrics_text.strip(), track_id),
                        )
                        await deps.db.commit()
                        # Store on mozaiklabs.fr for other installations
                        try:
                            async with aiohttp.ClientSession() as s2:
                                await s2.post(
                                    "https://mozaiklabs.fr/api/v1/lyrics",
                                    json={"title": title, "artist": artist,
                                          "album": track.album_title, "lyrics": lyrics_text.strip(),
                                          "source": "lrclib"},
                                    timeout=aiohttp.ClientTimeout(total=5),
                                )
                        except Exception:
                            pass
                        return {"lyrics": lyrics_text.strip(), "synced": synced.strip() if synced else None, "source": "lrclib"}
    except Exception:
        pass

    return {"lyrics": None, "source": None}


@router.get("/albums/{album_id}/bio")
async def get_album_bio(album_id: int):
    """Get album bio/liner notes. Checks DB cache first, then MusicBrainz, then mozaiklabs."""
    album = await deps.album_repo.get(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    # 1. Check DB cache
    try:
        row = await deps.db.fetchone("SELECT bio FROM albums WHERE id = ?", (album_id,))
        if row and row["bio"]:
            return {"bio": row["bio"], "source": "database"}
    except Exception:
        pass

    import aiohttp
    artist_name = album.artist_name or ""
    bio_text = None
    bio_source = None
    release_id = None

    # 2. Try MusicBrainz annotation (existing logic)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://musicbrainz.org/ws/2/release",
                params={"query": f'release:"{album.title}" AND artist:"{artist_name}"', "limit": 1, "fmt": "json"},
                headers={"User-Agent": "TuneServer/0.8.0"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    releases = data.get("releases", [])
                    if releases:
                        release_id = releases[0]["id"]
                        async with session.get(
                            f"https://musicbrainz.org/ws/2/release/{release_id}",
                            params={"inc": "annotation", "fmt": "json"},
                            headers={"User-Agent": "TuneServer/0.8.0"},
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp2:
                            if resp2.status == 200:
                                detail = await resp2.json()
                                annotation = detail.get("annotation", "")
                                if annotation:
                                    bio_text = annotation
                                    bio_source = "musicbrainz"
    except Exception:
        logger.debug("album_bio_musicbrainz_error", album_id=album_id)

    # 3. Fallback: mozaiklabs.fr
    if not bio_text:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://mozaiklabs.fr/api/v1/albums/bio",
                    params={"title": album.title, "artist": artist_name},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("bio"):
                            bio_text = data["bio"]
                            bio_source = "mozaiklabs"
        except Exception:
            logger.debug("album_bio_mozaiklabs_error", album_id=album_id)

    # 4. Save to DB if found
    if bio_text:
        try:
            await deps.album_repo.update_bio(album_id, bio_text)
        except Exception:
            logger.debug("album_bio_save_error", album_id=album_id)
        return {"bio": bio_text, "source": bio_source, "release_id": release_id}

    return {"bio": None, "source": None, "release_id": release_id}


@router.get("/artists/{artist_id}/timeline")
async def get_artist_timeline(artist_id: int):
    """Chronological discography timeline for an artist."""
    albums = await deps.album_repo.list_by_artist(artist_id)
    timeline = []
    for a in sorted(albums, key=lambda x: x.year or 9999):
        track_count = a.track_count or 0
        timeline.append({
            "album_id": a.id,
            "title": a.title,
            "year": a.year,
            "genre": a.genre,
            "cover_path": a.cover_path,
            "track_count": track_count,
            "format": a.format,
            "quality": a.quality,
        })
    return timeline


@router.get("/albums/{album_id}/similar")
async def get_similar_albums(album_id: int, limit: int = Query(10, le=30)):
    """Find similar albums in the library based on genre and artist."""
    album = await deps.album_repo.get(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    similar = []
    # Find albums with same genre
    if album.genre:
        genre_albums = await deps.album_repo.list_by_genre(album.genre)
        for a in genre_albums:
            if a.id != album_id and a not in similar:
                similar.append(a)
    # Find other albums by same artist
    if album.artist_id:
        artist_albums = await deps.album_repo.list_by_artist(album.artist_id)
        for a in artist_albums:
            if a.id != album_id and a not in similar:
                similar.append(a)

    return similar[:limit]


@router.get("/smart-playlists")
async def list_smart_playlists():
    from tune_server.db.repository import SmartPlaylistRepo
    repo = SmartPlaylistRepo(deps.db)
    return await repo.list()


@router.post("/smart-playlists")
async def create_smart_playlist(body: dict):
    from tune_server.db.repository import SmartPlaylistRepo
    repo = SmartPlaylistRepo(deps.db)
    import json
    sp_id = await repo.create(
        name=body["name"],
        rules=json.dumps(body.get("rules", [])),
        match_mode=body.get("match_mode", "all"),
        sort_by=body.get("sort_by", "title"),
        sort_order=body.get("sort_order", "asc"),
        max_tracks=body.get("max_tracks", 200),
        description=body.get("description"),
    )
    return {"id": sp_id}


@router.get("/smart-playlists/{sp_id}")
async def get_smart_playlist(sp_id: int):
    from tune_server.db.repository import SmartPlaylistRepo
    repo = SmartPlaylistRepo(deps.db)
    sp = await repo.get(sp_id)
    if not sp:
        raise HTTPException(status_code=404, detail="Smart playlist not found")
    return sp


@router.put("/smart-playlists/{sp_id}")
async def update_smart_playlist(sp_id: int, body: dict):
    from tune_server.db.repository import SmartPlaylistRepo
    repo = SmartPlaylistRepo(deps.db)
    import json
    updates = {}
    for key in ("name", "description", "match_mode", "sort_by", "sort_order", "max_tracks"):
        if key in body:
            updates[key] = body[key]
    if "rules" in body:
        updates["rules"] = json.dumps(body["rules"])
    await repo.update(sp_id, **updates)
    return await repo.get(sp_id)


@router.delete("/smart-playlists/{sp_id}")
async def delete_smart_playlist(sp_id: int):
    from tune_server.db.repository import SmartPlaylistRepo
    repo = SmartPlaylistRepo(deps.db)
    await repo.delete(sp_id)
    return {"deleted": sp_id}


@router.get("/smart-playlists/{sp_id}/tracks")
async def get_smart_playlist_tracks(sp_id: int):
    from tune_server.db.repository import SmartPlaylistRepo
    repo = SmartPlaylistRepo(deps.db)
    return await repo.resolve_tracks(sp_id)


@router.put("/tracks/{track_id}", response_model=Track)
async def update_track(track_id: int, req: TrackUpdateRequest):
    track = await deps.track_repo.get(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    updates = req.model_dump(exclude_none=True)

    # Write tags to file if relevant fields changed
    if track.file_path and track.source == "local":
        tag_updates = {}
        if req.title is not None:
            tag_updates["title"] = req.title
        if req.artist_id is not None:
            artist = await deps.artist_repo.get(req.artist_id)
            if artist:
                tag_updates["artist"] = artist.name
        if req.genre is not None:
            tag_updates["genre"] = req.genre
        if req.year is not None:
            tag_updates["year"] = req.year
        if req.track_number is not None:
            tag_updates["track_number"] = req.track_number
        if req.disc_number is not None:
            tag_updates["disc_number"] = req.disc_number
        if req.album_id is not None:
            album = await deps.album_repo.get(req.album_id)
            if album:
                tag_updates["album"] = album.title
        if tag_updates and not settings.metadata_readonly:
            await asyncio.to_thread(write_tags, track.file_path, **tag_updates)

    # Genre and year belong to the album, not the track
    album_updates = {}
    if "genre" in updates:
        album_updates["genre"] = updates.pop("genre")
    if "year" in updates:
        year_val = updates.pop("year")
        try:
            album_updates["year"] = int(year_val) if year_val else None
        except (ValueError, TypeError):
            album_updates["year"] = None

    for field, value in updates.items():
        setattr(track, field, value)
    await deps.track_repo.update(track)

    # Update album genre/year if provided
    if album_updates and track.album_id:
        album = await deps.album_repo.get(track.album_id)
        if album:
            if "genre" in album_updates:
                album.genre = album_updates["genre"]
            if "year" in album_updates:
                album.year = album_updates["year"]
            await deps.album_repo.update(album)

    return await deps.track_repo.get(track_id)


@router.delete("/tracks/{track_id}", status_code=204)
async def delete_track(track_id: int):
    track = await deps.track_repo.get(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    await deps.track_repo.delete(track_id)


@router.put("/albums/{album_id}", response_model=Album)
async def update_album(album_id: int, req: AlbumUpdateRequest):
    album = await deps.album_repo.get(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    updates = req.model_dump(exclude_none=True)

    # Write album tag to all tracks in the album (unless readonly)
    if req.title is not None and not settings.metadata_readonly:
        tracks = await deps.track_repo.list_by_album(album_id)
        for t in tracks:
            if t.file_path and t.source == "local":
                await asyncio.to_thread(write_tags, t.file_path, album=req.title)

    for field, value in updates.items():
        setattr(album, field, value)
    await deps.album_repo.update(album)
    return await deps.album_repo.get(album_id)


@router.post("/albums/merge-duplicates")
async def merge_duplicate_albums():
    """Merge albums that share normalized title + artist within the same
    quality tier. The repo decides keep/drop and handles duplicate track
    rows. Returns groups_merged, albums_deleted, details."""
    result = await deps.album_repo.merge_duplicates()
    if isinstance(result, int):
        # Legacy SQLite repo returns an int.
        return {"merged": result}
    return result


@router.delete("/albums/{album_id}", status_code=204)
async def delete_album(album_id: int):
    album = await deps.album_repo.get(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    await deps.album_repo.delete(album_id)


@router.put("/artists/{artist_id}", response_model=Artist)
async def update_artist(artist_id: int, req: ArtistUpdateRequest):
    artist = await deps.artist_repo.get(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    updates = req.model_dump(exclude_none=True)
    for field, value in updates.items():
        setattr(artist, field, value)
    await deps.artist_repo.update(artist)
    return await deps.artist_repo.get(artist_id)


@router.delete("/artists/{artist_id}", status_code=204)
async def delete_artist(artist_id: int):
    artist = await deps.artist_repo.get(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    await deps.artist_repo.delete(artist_id)


@router.get("/stats/completeness", response_model=CompletenessStats)
async def completeness_stats():
    # Count doubtful albums
    doubtful_row = await deps.db.fetchone(
        """SELECT count(*) as c FROM albums al
           LEFT JOIN artists ar ON al.artist_id = ar.id
           WHERE (al.artist_name = UPPER(al.artist_name) AND LENGTH(al.artist_name) > 4)
             OR LOWER(al.artist_name) IN ('inconnu', 'various artists', 'none')
             OR LOWER(al.genre) IN ('other', 'divers')
             OR (al.year IS NOT NULL AND al.year > 0 AND (al.year < 1920 OR al.year > 2026))
             OR (al.title = UPPER(al.title) AND LENGTH(al.title) > 4)
             OR (al.artist_name LIKE '____-%' OR al.artist_name LIKE '____ %')""",
    )
    return CompletenessStats(
        total_albums=await deps.album_repo.count(),
        albums_without_cover=await deps.album_repo.count_without_cover(),
        albums_without_genre=await deps.album_repo.count_without_genre(),
        albums_without_year=await deps.album_repo.count_without_year(),
        total_artists=await deps.artist_repo.count(),
        artists_without_image=await deps.artist_repo.count_without_image(),
        total_tracks=await deps.track_repo.count(),
        tracks_without_artist=await deps.track_repo.count_without_artist(),
        doubtful_count=doubtful_row["c"] if doubtful_row else 0,
    )


@router.get("/artwork/{filename:path}")
async def get_artwork(filename: str):
    """Serve artwork images from the cache directory."""
    # Try artwork_cache subdirectory first, then direct path
    cache_dir = Path(settings.artwork_cache_dir)
    file_path = cache_dir / filename
    if not file_path.exists():
        # Maybe filename includes "artwork_cache/" prefix
        file_path = Path(settings.artwork_cache_dir).parent / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Artwork not found")
    # Security: ensure the path is within the cache directory
    try:
        file_path.resolve().relative_to(cache_dir.resolve().parent)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    mime = "image/jpeg" if file_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return FileResponse(str(file_path), media_type=mime)


@router.post("/albums/{album_id}/artwork", response_model=Album)
async def upload_album_artwork(album_id: int, file: UploadFile):
    album = await deps.album_repo.get(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    image_data = await file.read()
    if not image_data:
        raise HTTPException(status_code=400, detail="Empty file")

    cover_path = await asyncio.to_thread(
        save_artwork, f"upload:album:{album_id}", image_data, True,
    )
    if not cover_path:
        raise HTTPException(status_code=500, detail="Failed to save artwork")

    album.cover_path = cover_path
    await deps.album_repo.update(album)

    # Copy cover.jpg to album folder
    tracks = await deps.track_repo.list_by_album(album_id)
    if tracks and tracks[0].file_path:
        await asyncio.to_thread(copy_cover_to_album_folder, cover_path, tracks[0].file_path)

    return await deps.album_repo.get(album_id)


@router.post("/albums/{album_id}/artwork/rescan")
async def rescan_album_artwork(album_id: int):
    album = await deps.album_repo.get(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    cover_path = None

    # Try local extraction from album tracks
    tracks = await deps.track_repo.list_by_album(album_id)
    for track in tracks:
        cover_path = await asyncio.to_thread(get_album_artwork, track.file_path)
        if cover_path:
            break

    # Fallback: MusicBrainz
    if not cover_path and album.artist_name and album.title:
        cover_path = await asyncio.to_thread(
            fetch_cover_from_musicbrainz,
            album.artist_name,
            album.title,
            settings.artwork_cache_dir,
        )

    if cover_path:
        album.cover_path = cover_path
        await deps.album_repo.update(album)

        # Copy cover.jpg to album folder
        if tracks and tracks[0].file_path:
            await asyncio.to_thread(copy_cover_to_album_folder, cover_path, tracks[0].file_path)

        return {"status": "found", "cover_path": cover_path}

    return {"status": "not_found", "cover_path": None}


_artwork_rescan_running = False


async def _rescan_artwork_task() -> None:
    global _artwork_rescan_running
    _artwork_rescan_running = True
    try:
        # Albums without cover + albums whose cover file is missing
        albums = await deps.album_repo.list_without_cover()
        all_with_cover = await deps.album_repo.list(limit=100000, offset=0)
        missing_ids = {a.id for a in albums}
        for album in all_with_cover:
            if album.id not in missing_ids and album.cover_path:
                if not Path(album.cover_path).exists():
                    albums.append(album)
        total = len(albums)
        found = 0
        logger.info("artwork_rescan_started", total=total)

        for i, album in enumerate(albums):
            # Try local extraction first (from any track of the album)
            tracks = await deps.track_repo.list_by_album(album.id)
            cover_path = None
            for track in tracks:
                cover_path = await asyncio.to_thread(get_album_artwork, track.file_path)
                if cover_path:
                    break

            # Fallback: MusicBrainz
            if not cover_path and album.artist_name and album.title:
                cover_path = await asyncio.to_thread(
                    fetch_cover_from_musicbrainz,
                    album.artist_name,
                    album.title,
                    settings.artwork_cache_dir,
                )

            if cover_path:
                album.cover_path = cover_path
                await deps.album_repo.update(album)
                found += 1

                # Copy cover.jpg to album folder
                if tracks and tracks[0].file_path:
                    await asyncio.to_thread(copy_cover_to_album_folder, cover_path, tracks[0].file_path)

            # Emit progress every album
            await deps.event_bus.emit(Event(
                type=EventType.LIBRARY_ARTWORK_PROGRESS,
                data={"current": i + 1, "total": total, "found": found},
                source="artwork_rescan",
            ))

        logger.info("artwork_rescan_completed", total=total, found=found)
        await deps.event_bus.emit(Event(
            type=EventType.LIBRARY_ARTWORK_COMPLETED,
            data={"total": total, "found": found},
            source="artwork_rescan",
        ))
    except Exception:
        logger.exception("artwork_rescan_error")
    finally:
        _artwork_rescan_running = False


@router.post("/artwork/rescan")
async def rescan_artwork():
    if _artwork_rescan_running:
        return {"status": "already_running"}
    asyncio.create_task(_rescan_artwork_task())
    return {"status": "started"}


# --- Offline Mode ---


@router.post("/offline/mark")
async def mark_offline(body: dict):
    """Mark tracks/playlists for offline availability."""
    track_ids = body.get("track_ids", [])
    playlist_id = body.get("playlist_id")
    album_id = body.get("album_id")

    marked = []

    if playlist_id:
        tracks = await deps.playlist_repo.get_tracks(playlist_id)
        track_ids.extend([t.id for t in tracks if t.id])

    if album_id:
        tracks = await deps.track_repo.list_by_album(album_id)
        track_ids.extend([t.id for t in tracks if t.id])

    for tid in set(track_ids):
        track = await deps.track_repo.get(tid)
        if track and track.file_path:
            marked.append({
                "track_id": tid,
                "title": track.title,
                "artist": track.artist_name,
                "file_path": track.file_path,
                "size_estimate_mb": round((track.duration_ms or 0) * 0.02 / 1000, 1),  # rough estimate
            })

    return {"marked": len(marked), "tracks": marked}


@router.get("/offline/status")
async def offline_status():
    """Get offline download status."""
    return {
        "available": False,  # Client-side feature — server provides file access
        "message": "Offline mode is managed by the client app. Use the /stream endpoint to download tracks.",
    }


# --- Smart Duplicate Detection ---

@router.get("/duplicates/smart")
async def smart_duplicates(limit: int = 50):
    """Detect albums that exist both locally and on streaming services.
    Suggests the best version based on quality."""
    # Find local albums
    local_albums = await deps.db.fetchall(
        """SELECT id, title, artist_name, format, sample_rate, bit_depth, source
           FROM albums WHERE source = 'local' OR source IS NULL
           ORDER BY title LIMIT 500""")

    duplicates = []
    for album in local_albums:
        title = album["title"]
        artist = album["artist_name"] or ""

        # Search for streaming versions
        streaming_matches = await deps.db.fetchall(
            """SELECT id, title, artist_name, format, sample_rate, bit_depth, source, source_id, cover_path
               FROM albums
               WHERE title LIKE ? AND source != 'local' AND source IS NOT NULL
               LIMIT 5""",
            (f"%{title}%",))

        for match in streaming_matches:
            # Compare quality
            local_quality = (album["sample_rate"] or 44100) * (album["bit_depth"] or 16)
            streaming_quality = (match["sample_rate"] or 44100) * (match["bit_depth"] or 16)

            best = "local" if local_quality >= streaming_quality else "streaming"

            duplicates.append({
                "local": {
                    "id": album["id"], "title": album["title"], "artist": artist,
                    "format": album["format"], "sample_rate": album["sample_rate"],
                    "bit_depth": album["bit_depth"],
                },
                "streaming": {
                    "id": match["id"], "title": match["title"], "artist": match["artist_name"],
                    "source": match["source"], "format": match["format"],
                    "sample_rate": match["sample_rate"], "bit_depth": match["bit_depth"],
                    "cover_path": match["cover_path"],
                },
                "best_version": best,
                "reason": f"Local: {album['format']} {album['sample_rate']}Hz/{album['bit_depth']}bit vs Streaming: {match['format']} {match['sample_rate']}Hz/{match['bit_depth']}bit"
            })

    return {"duplicates": duplicates[:limit], "total": len(duplicates)}


# --- Collections (Album Grouping) ---

@router.get("/collections")
async def list_collections():
    rows = await deps.db.fetchall(
        "SELECT c.*, COUNT(ca.album_id) as album_count FROM collections c LEFT JOIN collection_albums ca ON c.id = ca.collection_id GROUP BY c.id ORDER BY c.sort_order, c.name")
    return [dict(r) for r in rows]


@router.post("/collections")
async def create_collection(body: dict):
    name = body.get("name", "New Collection")
    description = body.get("description")
    icon = body.get("icon", "folder")
    color = body.get("color", "#6366f1")
    await deps.db.execute(
        "INSERT INTO collections (name, description, icon, color) VALUES (?, ?, ?, ?)",
        (name, description, icon, color))
    await deps.db.commit()
    row = await deps.db.fetchone("SELECT * FROM collections ORDER BY id DESC LIMIT 1")
    return dict(row)


@router.put("/collections/{collection_id}")
async def update_collection(collection_id: int, body: dict):
    name = body.get("name")
    description = body.get("description")
    icon = body.get("icon")
    color = body.get("color")
    if name:
        await deps.db.execute("UPDATE collections SET name = ? WHERE id = ?", (name, collection_id))
    if description is not None:
        await deps.db.execute("UPDATE collections SET description = ? WHERE id = ?", (description, collection_id))
    if icon:
        await deps.db.execute("UPDATE collections SET icon = ? WHERE id = ?", (icon, collection_id))
    if color:
        await deps.db.execute("UPDATE collections SET color = ? WHERE id = ?", (color, collection_id))
    await deps.db.commit()
    return {"updated": True}


@router.delete("/collections/{collection_id}")
async def delete_collection(collection_id: int):
    await deps.db.execute("DELETE FROM collection_albums WHERE collection_id = ?", (collection_id,))
    await deps.db.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
    await deps.db.commit()
    return {"deleted": True}


@router.get("/collections/{collection_id}/albums")
async def collection_albums(collection_id: int):
    rows = await deps.db.fetchall(
        """SELECT a.id, a.title, a.artist_name, a.year, a.genre, a.cover_path, a.format, a.sample_rate, a.bit_depth, ca.added_at
           FROM collection_albums ca JOIN albums a ON ca.album_id = a.id
           WHERE ca.collection_id = ? ORDER BY ca.added_at DESC""",
        (collection_id,))
    return [dict(r) for r in rows]


@router.post("/collections/{collection_id}/albums")
async def add_album_to_collection(collection_id: int, body: dict):
    album_id = body.get("album_id")
    if not album_id:
        raise HTTPException(400, "album_id required")
    try:
        await deps.db.execute(
            "INSERT INTO collection_albums (collection_id, album_id) VALUES (?, ?)",
            (collection_id, album_id))
        await deps.db.commit()
    except Exception:
        pass  # already exists
    return {"added": True, "collection_id": collection_id, "album_id": album_id}


@router.delete("/collections/{collection_id}/albums/{album_id}")
async def remove_album_from_collection(collection_id: int, album_id: int):
    await deps.db.execute(
        "DELETE FROM collection_albums WHERE collection_id = ? AND album_id = ?",
        (collection_id, album_id))
    await deps.db.commit()
    return {"removed": True}


# --- Import/Export Ratings ---

@router.get("/ratings/export")
async def export_ratings():
    """Export all ratings as JSON."""
    rows = await deps.db.fetchall(
        """SELECT ar.album_id, a.title, a.artist_name, ar.rating, ar.note, ar.updated_at
           FROM album_ratings ar JOIN albums a ON ar.album_id = a.id
           ORDER BY ar.rating DESC""")
    return {"ratings": [dict(r) for r in rows], "count": len(rows)}


@router.post("/ratings/import")
async def import_ratings(body: dict):
    """Import ratings from JSON array."""
    ratings = body.get("ratings", [])
    imported = 0
    for r in ratings:
        album_id = r.get("album_id")
        rating = r.get("rating")
        note = r.get("note")
        if album_id and rating:
            try:
                await deps.db.execute(
                    """INSERT INTO album_ratings (album_id, rating, note)
                       VALUES (?, ?, ?)
                       ON CONFLICT(album_id, profile_id) DO UPDATE SET rating=?, note=?""",
                    (album_id, rating, note, rating, note))
                imported += 1
            except Exception:
                pass
    await deps.db.commit()
    return {"imported": imported, "total": len(ratings)}


# --- Activity Feed ---

@router.get("/activity")
async def activity_feed(limit: int = 30):
    """Recent activity across all profiles/zones."""
    rows = await deps.db.fetchall(
        """SELECT ph.track_title, ph.artist_name, ph.album_title, ph.cover_path,
                  ph.source, ph.played_at, ph.zone_id
           FROM playback_history ph
           ORDER BY ph.played_at DESC
           LIMIT ?""",
        (limit,))

    # Get zone names
    zone_names = {}
    if deps.zone_manager:
        for z in deps.zone_manager.list_zones():
            zone_names[z.zone_id] = z.name

    return [{
        "track_title": r["track_title"],
        "artist_name": r["artist_name"],
        "album_title": r["album_title"],
        "cover_path": r["cover_path"],
        "source": r["source"],
        "played_at": r["played_at"],
        "zone_name": zone_names.get(r["zone_id"], f"Zone {r['zone_id']}"),
    } for r in rows]


# --- Browse by directory ---


def _is_path_under_roots(path: str) -> str | None:
    """Validate that path is under a configured music_dir. Returns the matching root or None."""
    try:
        resolved = Path(path).resolve()
    except (ValueError, OSError):
        return None
    for music_dir in settings.music_dirs:
        root = Path(music_dir).resolve()
        if resolved == root or str(resolved).startswith(str(root) + os.sep):
            return str(root)
    return None


@router.get("/browse", response_model=BrowseRootsResponse)
async def browse_roots():
    """List configured music directories with track counts."""
    # Build mount_path → friendly name from network mounts + discovered devices
    mount_display: dict[str, str] = {}
    if deps.mount_manager:
        mounts = await deps.mount_manager.list_mounts()
        # host → device name from discovered DLNA renderers
        device_names: dict[str, str] = {}
        if deps.discovery_manager:
            for dev in deps.discovery_manager.list_devices():
                if dev.host:
                    device_names[dev.host] = dev.name
        for m in mounts:
            if m.status == "mounted":
                name = device_names.get(m.host, m.host)
                mount_display[m.mount_path] = name

    roots = []
    for music_dir in settings.music_dirs:
        resolved = str(Path(music_dir).resolve()).replace("\\", "/")
        count = await deps.track_repo.count_by_root(resolved)
        name = mount_display.get(resolved, Path(resolved).name)
        roots.append(BrowseRootEntry(
            name=name,
            path=resolved,
            track_count=count,
        ))
    return BrowseRootsResponse(roots=roots)


@router.get("/browse/dir", response_model=BrowseResult)
async def browse_directory(path: str = Query(..., description="Absolute path to browse")):
    """Browse a directory: returns subdirectories and tracks."""
    music_root = _is_path_under_roots(path)
    if music_root is None:
        raise HTTPException(status_code=403, detail="Path is not under a configured music directory")

    resolved = str(Path(path).resolve())

    subdirs = await deps.track_repo.list_subdirectories(resolved)
    tracks = await deps.track_repo.list_by_directory(resolved)

    directories = [
        BrowseDirectory(name=d["name"], path=d["path"], track_count=d["track_count"])
        for d in subdirs
    ]

    # Compute parent (None if we're at the music root)
    parent = None
    if resolved != music_root:
        parent = str(Path(resolved).parent)

    return BrowseResult(
        path=resolved,
        parent=parent,
        music_root=music_root,
        directories=directories,
        tracks=tracks,
    )

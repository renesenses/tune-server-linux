"""User profiles and favorites API routes."""
from __future__ import annotations

import structlog

from fastapi import APIRouter, HTTPException, Query

from tune_server.api.deps import deps
from tune_server.models import (
    Album, Artist, Track, UserFavoriteAdd, UserFavoritesResponse,
    UserProfile, UserProfileCreate,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/profiles", tags=["profiles"])

# In-memory cache of active profile id (loaded from DB on first access)
_active_profile_id: int | None = None
_active_loaded: bool = False


# ---------------------------------------------------------------------------
# Profiles CRUD
# ---------------------------------------------------------------------------

@router.get("")
async def list_profiles() -> list[UserProfile]:
    rows = await deps.db.fetchall(
        "SELECT * FROM user_profiles ORDER BY name"
    )
    return [UserProfile(**_clean_row(r)) for r in rows]


@router.post("", status_code=201)
async def create_profile(body: UserProfileCreate) -> UserProfile:
    # Check for existing profile (case-insensitive)
    existing = await deps.db.fetchone(
        "SELECT * FROM user_profiles WHERE LOWER(name) = LOWER(?)",
        (body.name.strip(),),
    )
    if existing:
        raise HTTPException(
            409,
            detail={
                "error": "profile_exists",
                "message": f"A profile named '{existing['name']}' already exists",
                "existing_profile": UserProfile(**_clean_row(existing)).model_dump(),
            },
        )
    result = await deps.db.execute(
        "INSERT INTO user_profiles (name, avatar_color) VALUES (?, ?) RETURNING id",
        (body.name.strip(), body.avatar_color),
    )
    await deps.db.commit()
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (result.lastrowid,))
    return UserProfile(**_clean_row(row))


@router.get("/search")
async def search_profiles(q: str = Query(..., min_length=1)) -> list[UserProfile]:
    """Search profiles by name (case-insensitive, partial match)."""
    rows = await deps.db.fetchall(
        "SELECT * FROM user_profiles WHERE LOWER(name) LIKE LOWER(?) ORDER BY name",
        (f"%{q.strip()}%",),
    )
    return [UserProfile(**_clean_row(r)) for r in rows]


@router.get("/active")
async def get_active_profile():
    """Get the currently active profile."""
    pid = await _load_active_profile()
    if pid is None:
        return {"active_profile_id": None, "profile": None}
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (pid,))
    if not row:
        return {"active_profile_id": None, "profile": None}
    return {
        "active_profile_id": pid,
        "profile": UserProfile(**_clean_row(row)),
    }


@router.post("/deactivate")
async def deactivate_profile():
    """Clear the active profile (revert to no-profile mode)."""
    global _active_profile_id
    _active_profile_id = None
    await _persist_active_profile(None)
    return {"active_profile_id": None}


@router.get("/{profile_id}")
async def get_profile(profile_id: int) -> UserProfile:
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    return UserProfile(**_clean_row(row))


@router.put("/{profile_id}")
async def update_profile(profile_id: int, body: UserProfileCreate) -> UserProfile:
    # Check name collision (exclude current profile)
    existing = await deps.db.fetchone(
        "SELECT * FROM user_profiles WHERE LOWER(name) = LOWER(?) AND id != ?",
        (body.name.strip(), profile_id),
    )
    if existing:
        raise HTTPException(
            409,
            detail={
                "error": "profile_exists",
                "message": f"A profile named '{existing['name']}' already exists",
            },
        )
    await deps.db.execute(
        "UPDATE user_profiles SET name = ?, avatar_color = ? WHERE id = ?",
        (body.name.strip(), body.avatar_color, profile_id),
    )
    await deps.db.commit()
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    return UserProfile(**_clean_row(row))


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(profile_id: int):
    global _active_profile_id
    await deps.db.execute("DELETE FROM user_profiles WHERE id = ?", (profile_id,))
    await deps.db.commit()
    # If the deleted profile was active, clear it
    if _active_profile_id == profile_id:
        _active_profile_id = None
        await _persist_active_profile(None)


# ---------------------------------------------------------------------------
# Active profile
# ---------------------------------------------------------------------------

async def _load_active_profile() -> int | None:
    """Load active profile ID from DB (streaming_auth table)."""
    global _active_profile_id, _active_loaded
    if _active_loaded:
        return _active_profile_id
    try:
        row = await deps.db.fetchone(
            "SELECT token_data FROM streaming_auth WHERE service = ?",
            ("active_profile",),
        )
        if row:
            import json
            data = json.loads(row["token_data"])
            _active_profile_id = data.get("profile_id")
        _active_loaded = True
    except Exception:
        _active_loaded = True
    return _active_profile_id


async def _persist_active_profile(profile_id: int | None) -> None:
    """Persist active profile ID to DB."""
    import json
    data = json.dumps({"profile_id": profile_id})
    try:
        await deps.db.execute(
            """INSERT INTO streaming_auth (service, token_data) VALUES (?, ?)
               ON CONFLICT(service) DO UPDATE SET token_data = ?, updated_at = CURRENT_TIMESTAMP""",
            ("active_profile", data, data),
        )
        await deps.db.commit()
    except Exception:
        logger.exception("active_profile_persist_error")


@router.post("/{profile_id}/activate")
async def activate_profile(profile_id: int):
    """Switch the active profile for the session."""
    global _active_profile_id
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    _active_profile_id = profile_id
    await _persist_active_profile(profile_id)
    logger.info("profile_activated", profile_id=profile_id, name=row["name"])
    return {
        "active_profile_id": profile_id,
        "profile": UserProfile(**_clean_row(row)),
    }


# ---------------------------------------------------------------------------
# Profile with stats (enhanced GET)
# ---------------------------------------------------------------------------

@router.get("/{profile_id}/stats")
async def get_profile_stats(profile_id: int):
    """Get profile with aggregated stats (favorite counts, listening history)."""
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    profile = UserProfile(**_clean_row(row))

    # Count favorites by type
    fav_tracks = await deps.db.fetchone(
        "SELECT COUNT(*) as cnt FROM user_favorites WHERE user_id = ? AND track_id IS NOT NULL",
        (profile_id,),
    )
    fav_albums = await deps.db.fetchone(
        "SELECT COUNT(*) as cnt FROM user_favorites WHERE user_id = ? AND album_id IS NOT NULL",
        (profile_id,),
    )
    fav_artists = await deps.db.fetchone(
        "SELECT COUNT(*) as cnt FROM user_favorites WHERE user_id = ? AND artist_id IS NOT NULL",
        (profile_id,),
    )

    # Listening history count (if playback_history exists)
    history_count = 0
    try:
        hist_row = await deps.db.fetchone(
            "SELECT COUNT(*) as cnt FROM playback_history WHERE profile_id = ?",
            (profile_id,),
        )
        if hist_row:
            history_count = hist_row["cnt"]
    except Exception:
        pass  # Table may not have profile_id column yet

    return {
        "profile": profile,
        "stats": {
            "favorite_tracks": fav_tracks["cnt"] if fav_tracks else 0,
            "favorite_albums": fav_albums["cnt"] if fav_albums else 0,
            "favorite_artists": fav_artists["cnt"] if fav_artists else 0,
            "listening_history": history_count,
        },
    }


# ---------------------------------------------------------------------------
# Favorites
# ---------------------------------------------------------------------------

@router.get("/{profile_id}/favorites")
async def get_favorites(
    profile_id: int,
    type: str | None = Query(None, description="Filter: track, album, artist"),
) -> UserFavoritesResponse:
    tracks: list[Track] = []
    albums: list[Album] = []
    artists: list[Artist] = []

    if type is None or type == "track":
        rows = await deps.db.fetchall(
            """SELECT t.*, al.title as album_title, ar.name as artist_name, al.cover_path as cover_path
               FROM user_favorites f
               JOIN tracks t ON f.track_id = t.id
               LEFT JOIN albums al ON t.album_id = al.id
               LEFT JOIN artists ar ON t.artist_id = ar.id
               WHERE f.user_id = ? AND f.track_id IS NOT NULL
               ORDER BY f.created_at DESC""",
            (profile_id,),
        )
        tracks = [Track(**_track_from_row(r)) for r in rows]

    if type is None or type == "album":
        rows = await deps.db.fetchall(
            """SELECT al.*, ar.name as artist_name
               FROM user_favorites f
               JOIN albums al ON f.album_id = al.id
               LEFT JOIN artists ar ON al.artist_id = ar.id
               WHERE f.user_id = ? AND f.album_id IS NOT NULL
               ORDER BY f.created_at DESC""",
            (profile_id,),
        )
        albums = [Album(**_album_from_row(r)) for r in rows]

    if type is None or type == "artist":
        rows = await deps.db.fetchall(
            """SELECT a.*
               FROM user_favorites f
               JOIN artists a ON f.artist_id = a.id
               WHERE f.user_id = ? AND f.artist_id IS NOT NULL
               ORDER BY f.created_at DESC""",
            (profile_id,),
        )
        artists = [Artist(**dict(r)) for r in rows]

    return UserFavoritesResponse(tracks=tracks, albums=albums, artists=artists)


@router.post("/{profile_id}/favorites", status_code=201)
async def add_favorite(profile_id: int, body: UserFavoriteAdd) -> dict:
    if not body.track_id and not body.album_id and not body.artist_id:
        raise HTTPException(400, "Provide track_id, album_id, or artist_id")
    try:
        if body.track_id:
            await deps.db.execute(
                "INSERT INTO user_favorites (user_id, track_id) VALUES (?, ?)",
                (profile_id, body.track_id),
            )
        elif body.album_id:
            await deps.db.execute(
                "INSERT INTO user_favorites (user_id, album_id) VALUES (?, ?)",
                (profile_id, body.album_id),
            )
        elif body.artist_id:
            await deps.db.execute(
                "INSERT INTO user_favorites (user_id, artist_id) VALUES (?, ?)",
                (profile_id, body.artist_id),
            )
    except Exception:
        pass  # Duplicate — ignore
    await deps.db.commit()
    return {"status": "added"}


@router.delete("/{profile_id}/favorites")
async def remove_favorite(
    profile_id: int,
    track_id: int | None = Query(None),
    album_id: int | None = Query(None),
    artist_id: int | None = Query(None),
) -> dict:
    if track_id:
        await deps.db.execute(
            "DELETE FROM user_favorites WHERE user_id = ? AND track_id = ?",
            (profile_id, track_id),
        )
    elif album_id:
        await deps.db.execute(
            "DELETE FROM user_favorites WHERE user_id = ? AND album_id = ?",
            (profile_id, album_id),
        )
    elif artist_id:
        await deps.db.execute(
            "DELETE FROM user_favorites WHERE user_id = ? AND artist_id = ?",
            (profile_id, artist_id),
        )
    await deps.db.commit()
    return {"status": "removed"}


@router.get("/{profile_id}/favorites/check")
async def check_favorite(
    profile_id: int,
    track_id: int | None = Query(None),
    album_id: int | None = Query(None),
    artist_id: int | None = Query(None),
) -> dict:
    """Check if an item is favorited."""
    if track_id:
        row = await deps.db.fetchone(
            "SELECT id FROM user_favorites WHERE user_id = ? AND track_id = ?",
            (profile_id, track_id),
        )
    elif album_id:
        row = await deps.db.fetchone(
            "SELECT id FROM user_favorites WHERE user_id = ? AND album_id = ?",
            (profile_id, album_id),
        )
    elif artist_id:
        row = await deps.db.fetchone(
            "SELECT id FROM user_favorites WHERE user_id = ? AND artist_id = ?",
            (profile_id, artist_id),
        )
    else:
        return {"is_favorite": False}
    return {"is_favorite": row is not None}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_row(row) -> dict:
    """Convert row to dict, stringify datetime fields for Pydantic."""
    d = dict(row)
    for k, v in d.items():
        if v is not None and not isinstance(v, (str, int, float, bool)):
            d[k] = str(v)
    return d


def _track_from_row(row) -> dict:
    d = dict(row)
    keys = row.keys()
    if "album_title" in keys:
        d["album_title"] = row["album_title"]
    if "artist_name" in keys:
        d["artist_name"] = row["artist_name"]
    if "cover_path" in keys and row["cover_path"]:
        d["cover_path"] = row["cover_path"]
    return d


def _album_from_row(row) -> dict:
    d = dict(row)
    keys = row.keys()
    if "artist_name" in keys:
        d["artist_name"] = row["artist_name"]
    return d

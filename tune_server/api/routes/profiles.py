"""User profiles and favorites API routes.

Multi-user authentication with separate profiles.  Each profile has its own
favorites, play history, EQ settings, and quality preferences.  An optional
PIN protects profile switching.  A default "Admin" profile is seeded on
first server start.
"""
from __future__ import annotations

import hashlib
import secrets

import structlog

from fastapi import APIRouter, HTTPException, Query

from tune_server.api.deps import deps
from tune_server.models import (
    Album, Artist, ProfileSwitchRequest, Track,
    UserFavoriteAdd, UserFavoritesResponse,
    UserProfile, UserProfileCreate,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/profiles", tags=["profiles"])

# In-memory cache of active profile id (loaded from DB on first access)
_active_profile_id: int | None = None
_active_loaded: bool = False


# ---------------------------------------------------------------------------
# PIN hashing helpers
# ---------------------------------------------------------------------------

def _hash_pin(pin: str) -> str:
    """Hash a PIN with a random salt.  Format: ``salt$sha256hex``."""
    salt = secrets.token_hex(8)
    digest = hashlib.sha256(f"{salt}${pin}".encode()).hexdigest()
    return f"{salt}${digest}"


def _verify_pin(pin: str, pin_hash: str) -> bool:
    """Verify *pin* against a stored ``salt$sha256hex`` hash."""
    if not pin_hash or "$" not in pin_hash:
        return False
    salt, expected = pin_hash.split("$", 1)
    digest = hashlib.sha256(f"{salt}${pin}".encode()).hexdigest()
    return secrets.compare_digest(digest, expected)


# ---------------------------------------------------------------------------
# Default Admin profile seeding
# ---------------------------------------------------------------------------

async def seed_default_admin_profile() -> None:
    """Create the default Admin profile on first start (idempotent)."""
    try:
        row = await deps.db.fetchone(
            "SELECT id FROM user_profiles WHERE is_admin = 1 LIMIT 1"
        )
        if row:
            return  # Admin already exists

        # Also skip if any profile exists at all (upgrade from pre-multi-user)
        count_row = await deps.db.fetchone(
            "SELECT COUNT(*) as cnt FROM user_profiles"
        )
        if count_row and count_row["cnt"] > 0:
            # Promote the first existing profile to admin
            first = await deps.db.fetchone(
                "SELECT id FROM user_profiles ORDER BY id LIMIT 1"
            )
            if first:
                await deps.db.execute(
                    "UPDATE user_profiles SET is_admin = 1 WHERE id = ?",
                    (first["id"],),
                )
                await deps.db.commit()
                logger.info("existing_profile_promoted_to_admin", profile_id=first["id"])
            return

        await deps.db.execute(
            """INSERT INTO user_profiles (name, avatar_color, is_admin)
               VALUES (?, ?, 1)""",
            ("Admin", "#FF6B35"),
        )
        await deps.db.commit()

        # Auto-activate the Admin profile
        admin = await deps.db.fetchone(
            "SELECT id FROM user_profiles WHERE is_admin = 1 LIMIT 1"
        )
        if admin:
            global _active_profile_id, _active_loaded
            _active_profile_id = admin["id"]
            _active_loaded = True
            await _persist_active_profile(admin["id"])
            logger.info("default_admin_profile_created", profile_id=admin["id"])
    except Exception:
        logger.exception("seed_admin_profile_failed")


# ---------------------------------------------------------------------------
# Profiles CRUD
# ---------------------------------------------------------------------------

@router.get("")
async def list_profiles() -> list[UserProfile]:
    rows = await deps.db.fetchall(
        "SELECT * FROM user_profiles ORDER BY name"
    )
    return [_row_to_profile(r) for r in rows]


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
                "existing_profile": _row_to_profile(existing).model_dump(),
            },
        )
    pin_hash = _hash_pin(body.pin) if body.pin else None
    result = await deps.db.execute(
        """INSERT INTO user_profiles
           (name, avatar_color, avatar_url, pin_hash, eq_settings, quality_preference)
           VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
        (body.name.strip(), body.avatar_color, body.avatar_url,
         pin_hash, body.eq_settings, body.quality_preference),
    )
    await deps.db.commit()
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (result.lastrowid,))
    return _row_to_profile(row)


@router.get("/search")
async def search_profiles(q: str = Query(..., min_length=1)) -> list[UserProfile]:
    """Search profiles by name (case-insensitive, partial match)."""
    rows = await deps.db.fetchall(
        "SELECT * FROM user_profiles WHERE LOWER(name) LIKE LOWER(?) ORDER BY name",
        (f"%{q.strip()}%",),
    )
    return [_row_to_profile(r) for r in rows]


# ---------------------------------------------------------------------------
# Active / current profile
# ---------------------------------------------------------------------------

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
        "profile": _row_to_profile(row),
    }


@router.get("/current")
async def get_current_profile():
    """Get the current active profile (alias for /active)."""
    return await get_active_profile()


@router.post("/switch")
async def switch_profile(body: ProfileSwitchRequest):
    """Switch active profile by id, with optional PIN verification."""
    row = await deps.db.fetchone(
        "SELECT * FROM user_profiles WHERE id = ?", (body.profile_id,)
    )
    if not row:
        raise HTTPException(404, "Profile not found")

    keys = row.keys() if hasattr(row, "keys") else []
    pin_hash = row["pin_hash"] if "pin_hash" in keys else None

    # Verify PIN if the target profile has one set
    if pin_hash:
        if not body.pin:
            raise HTTPException(403, detail={
                "error": "pin_required",
                "message": "This profile requires a PIN to switch",
            })
        if not _verify_pin(body.pin, pin_hash):
            raise HTTPException(403, detail={
                "error": "invalid_pin",
                "message": "Incorrect PIN",
            })

    global _active_profile_id
    _active_profile_id = body.profile_id
    await _persist_active_profile(body.profile_id)
    logger.info("profile_switched", profile_id=body.profile_id, name=row["name"])
    return {
        "active_profile_id": body.profile_id,
        "profile": _row_to_profile(row),
    }


@router.post("/deactivate")
async def deactivate_profile():
    """Clear the active profile (revert to no-profile mode)."""
    global _active_profile_id
    _active_profile_id = None
    await _persist_active_profile(None)
    return {"active_profile_id": None}


# ---------------------------------------------------------------------------
# Single profile CRUD (by id)
# ---------------------------------------------------------------------------

@router.get("/{profile_id}")
async def get_profile(profile_id: int) -> UserProfile:
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    return _row_to_profile(row)


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
    # Build update — only update pin_hash if a new PIN is provided
    pin_clause = ""
    params: list = [body.name.strip(), body.avatar_color, body.avatar_url,
                    body.eq_settings, body.quality_preference]
    if body.pin is not None:
        if body.pin == "":
            # Empty string = remove PIN
            pin_clause = ", pin_hash = ?"
            params.append(None)
        else:
            pin_clause = ", pin_hash = ?"
            params.append(_hash_pin(body.pin))

    params.append(profile_id)
    await deps.db.execute(
        f"""UPDATE user_profiles
            SET name = ?, avatar_color = ?, avatar_url = ?,
                eq_settings = ?, quality_preference = ?{pin_clause}
            WHERE id = ?""",
        tuple(params),
    )
    await deps.db.commit()
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    return _row_to_profile(row)


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(profile_id: int):
    global _active_profile_id
    # Prevent deleting the last admin profile
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    keys = row.keys() if hasattr(row, "keys") else []
    is_admin = bool(row["is_admin"]) if "is_admin" in keys else False
    if is_admin:
        admin_count = await deps.db.fetchone(
            "SELECT COUNT(*) as cnt FROM user_profiles WHERE is_admin = 1"
        )
        if admin_count and admin_count["cnt"] <= 1:
            raise HTTPException(400, "Cannot delete the last admin profile")

    await deps.db.execute("DELETE FROM user_profiles WHERE id = ?", (profile_id,))
    await deps.db.commit()
    # If the deleted profile was active, clear it
    if _active_profile_id == profile_id:
        _active_profile_id = None
        await _persist_active_profile(None)


@router.post("/{profile_id}/activate")
async def activate_profile(profile_id: int):
    """Switch the active profile for the session (legacy endpoint)."""
    global _active_profile_id
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    _active_profile_id = profile_id
    await _persist_active_profile(profile_id)
    logger.info("profile_activated", profile_id=profile_id, name=row["name"])
    return {
        "active_profile_id": profile_id,
        "profile": _row_to_profile(row),
    }


# ---------------------------------------------------------------------------
# Profile settings (EQ + quality)
# ---------------------------------------------------------------------------

@router.get("/{profile_id}/settings")
async def get_profile_settings(profile_id: int):
    """Get per-profile settings (EQ, quality preference)."""
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    keys = row.keys() if hasattr(row, "keys") else []
    import json
    eq = None
    raw_eq = row["eq_settings"] if "eq_settings" in keys else None
    if raw_eq:
        try:
            eq = json.loads(raw_eq)
        except (json.JSONDecodeError, TypeError):
            eq = raw_eq
    return {
        "profile_id": profile_id,
        "eq_settings": eq,
        "quality_preference": row["quality_preference"] if "quality_preference" in keys else None,
    }


@router.put("/{profile_id}/settings")
async def update_profile_settings(profile_id: int, body: dict):
    """Update per-profile settings.  Accepts eq_settings (object/string)
    and quality_preference (string)."""
    import json
    row = await deps.db.fetchone("SELECT id FROM user_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")

    updates = []
    params: list = []
    if "eq_settings" in body:
        eq = body["eq_settings"]
        updates.append("eq_settings = ?")
        params.append(json.dumps(eq) if isinstance(eq, (dict, list)) else eq)
    if "quality_preference" in body:
        updates.append("quality_preference = ?")
        params.append(body["quality_preference"])
    if not updates:
        raise HTTPException(400, "No settings provided")
    params.append(profile_id)
    await deps.db.execute(
        f"UPDATE user_profiles SET {', '.join(updates)} WHERE id = ?",
        tuple(params),
    )
    await deps.db.commit()
    return await get_profile_settings(profile_id)


# ---------------------------------------------------------------------------
# Profile with stats (enhanced GET)
# ---------------------------------------------------------------------------

@router.get("/{profile_id}/stats")
async def get_profile_stats(profile_id: int):
    """Get profile with aggregated stats (favorite counts, listening history)."""
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    profile = _row_to_profile(row)

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
            "SELECT COUNT(*) as cnt FROM playback_history WHERE user_id = ?",
            (profile_id,),
        )
        if hist_row:
            history_count = hist_row["cnt"]
    except Exception:
        pass  # Table may not have user_id column yet

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
# Play history (per-profile)
# ---------------------------------------------------------------------------

@router.get("/{profile_id}/history")
async def get_profile_history(profile_id: int, limit: int = Query(50, ge=1, le=500)):
    """Get play history for a specific profile."""
    try:
        rows = await deps.db.fetchall(
            """SELECT * FROM playback_history
               WHERE user_id = ?
               ORDER BY played_at DESC LIMIT ?""",
            (profile_id, limit),
        )
        return [dict(r) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Active profile persistence
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_profile(row) -> UserProfile:
    """Convert a DB row to a UserProfile model, handling missing columns
    gracefully (for DBs not yet migrated)."""
    keys = row.keys() if hasattr(row, "keys") else []
    d = dict(row)
    # Stringify non-primitive fields (datetime, etc.)
    for k, v in d.items():
        if v is not None and not isinstance(v, (str, int, float, bool)):
            d[k] = str(v)
    return UserProfile(
        id=d.get("id"),
        name=d.get("name", ""),
        avatar_color=d.get("avatar_color", "#FF6B35"),
        avatar_url=d.get("avatar_url") if "avatar_url" in keys else None,
        is_admin=bool(d.get("is_admin", 0)) if "is_admin" in keys else False,
        has_pin=bool(d.get("pin_hash")) if "pin_hash" in keys else False,
        eq_settings=d.get("eq_settings") if "eq_settings" in keys else None,
        quality_preference=d.get("quality_preference") if "quality_preference" in keys else None,
        created_at=d.get("created_at"),
    )


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

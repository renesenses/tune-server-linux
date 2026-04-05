"""User profiles and favorites API routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from tune_server.api.deps import deps
from tune_server.models import (
    Album, Artist, Track, UserFavoriteAdd, UserFavoritesResponse,
    UserProfile, UserProfileCreate,
)

router = APIRouter(prefix="/profiles", tags=["profiles"])


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
    result = await deps.db.execute(
        "INSERT INTO user_profiles (name, avatar_color) VALUES (?, ?) RETURNING id",
        (body.name, body.avatar_color),
    )
    await deps.db.commit()
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (result.lastrowid,))
    return UserProfile(**_clean_row(row))


@router.get("/{profile_id}")
async def get_profile(profile_id: int) -> UserProfile:
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    return UserProfile(**_clean_row(row))


@router.put("/{profile_id}")
async def update_profile(profile_id: int, body: UserProfileCreate) -> UserProfile:
    await deps.db.execute(
        "UPDATE user_profiles SET name = ?, avatar_color = ? WHERE id = ?",
        (body.name, body.avatar_color, profile_id),
    )
    await deps.db.commit()
    row = await deps.db.fetchone("SELECT * FROM user_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    return UserProfile(**_clean_row(row))


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(profile_id: int):
    await deps.db.execute("DELETE FROM user_profiles WHERE id = ?", (profile_id,))
    await deps.db.commit()


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
    else:
        raise HTTPException(400, "Provide track_id, album_id, or artist_id")
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

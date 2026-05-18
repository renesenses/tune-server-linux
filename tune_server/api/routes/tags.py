"""User-defined tags/labels for tracks, albums, and artists."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tune_server.api.deps import deps

logger = structlog.get_logger()

router = APIRouter(prefix="/tags", tags=["tags"])

# Secondary router for library-scoped tag lookups (mounted alongside library routes)
library_router = APIRouter(prefix="/library", tags=["tags"])

VALID_ITEM_TYPES = {"track", "album", "artist"}


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class TagCreate(BaseModel):
    name: str
    color: str = "#6366f1"
    icon: str | None = None


class TagUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    icon: str | None = None


class TagItemAdd(BaseModel):
    type: str  # 'track', 'album', 'artist'
    id: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db():
    if deps.db is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    return deps.db


def _validate_item_type(item_type: str) -> str:
    if item_type not in VALID_ITEM_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid item_type '{item_type}'. Must be one of: {', '.join(sorted(VALID_ITEM_TYPES))}",
        )
    return item_type


async def _get_tag_or_404(tag_id: int) -> dict:
    row = await _db().fetchone("SELECT * FROM user_tags WHERE id = ?", (tag_id,))
    if not row:
        raise HTTPException(status_code=404, detail="tag_not_found")
    return dict(row)


async def _tag_with_counts(tag: dict) -> dict:
    """Enrich a tag row with per-type item counts."""
    db = _db()
    rows = await db.fetchall(
        "SELECT item_type, COUNT(*) as cnt FROM user_tag_items WHERE tag_id = ? GROUP BY item_type",
        (tag["id"],),
    )
    counts = {r["item_type"]: r["cnt"] for r in rows}
    total = sum(counts.values())
    return {
        **tag,
        "item_counts": counts,
        "total_items": total,
    }


# ---------------------------------------------------------------------------
# Tag CRUD
# ---------------------------------------------------------------------------

@router.get("")
async def list_tags() -> list[dict]:
    """List all user tags with per-type item counts."""
    db = _db()
    rows = await db.fetchall("SELECT * FROM user_tags ORDER BY name")
    tags = [dict(r) for r in rows]
    result = []
    for tag in tags:
        result.append(await _tag_with_counts(tag))
    return result


@router.post("", status_code=201)
async def create_tag(body: TagCreate) -> dict:
    """Create a new tag."""
    db = _db()
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Tag name cannot be empty")

    existing = await db.fetchone(
        "SELECT id FROM user_tags WHERE LOWER(name) = LOWER(?)", (name,)
    )
    if existing:
        raise HTTPException(status_code=409, detail="tag_already_exists")

    result = await db.execute(
        "INSERT INTO user_tags (name, color, icon) VALUES (?, ?, ?)",
        (name, body.color, body.icon),
    )
    await db.commit()
    tag = await _get_tag_or_404(result.lastrowid)
    return await _tag_with_counts(tag)


@router.get("/{tag_id}")
async def get_tag(tag_id: int) -> dict:
    """Get a single tag with item counts."""
    tag = await _get_tag_or_404(tag_id)
    return await _tag_with_counts(tag)


@router.put("/{tag_id}")
async def update_tag(tag_id: int, body: TagUpdate) -> dict:
    """Update tag name, color, or icon."""
    db = _db()
    await _get_tag_or_404(tag_id)

    updates = []
    params = []
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Tag name cannot be empty")
        # Check uniqueness (case-insensitive) excluding self
        dup = await db.fetchone(
            "SELECT id FROM user_tags WHERE LOWER(name) = LOWER(?) AND id != ?",
            (name, tag_id),
        )
        if dup:
            raise HTTPException(status_code=409, detail="tag_already_exists")
        updates.append("name = ?")
        params.append(name)
    if body.color is not None:
        updates.append("color = ?")
        params.append(body.color)
    if body.icon is not None:
        updates.append("icon = ?")
        params.append(body.icon)

    if updates:
        params.append(tag_id)
        await db.execute(
            f"UPDATE user_tags SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
        await db.commit()

    tag = await _get_tag_or_404(tag_id)
    return await _tag_with_counts(tag)


@router.delete("/{tag_id}")
async def delete_tag(tag_id: int) -> dict:
    """Delete a tag and all its item associations."""
    db = _db()
    await _get_tag_or_404(tag_id)
    await db.execute("DELETE FROM user_tags WHERE id = ?", (tag_id,))
    await db.commit()
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Tag-item associations
# ---------------------------------------------------------------------------

@router.post("/{tag_id}/items", status_code=201)
async def add_item_to_tag(tag_id: int, body: TagItemAdd) -> dict:
    """Add a library item (track/album/artist) to a tag."""
    db = _db()
    await _get_tag_or_404(tag_id)
    item_type = _validate_item_type(body.type)

    # Verify the item exists
    table = {"track": "tracks", "album": "albums", "artist": "artists"}[item_type]
    item = await db.fetchone(f"SELECT id FROM {table} WHERE id = ?", (body.id,))
    if not item:
        raise HTTPException(status_code=404, detail=f"{item_type}_not_found")

    # Upsert — ignore if already tagged
    existing = await db.fetchone(
        "SELECT id FROM user_tag_items WHERE tag_id = ? AND item_type = ? AND item_id = ?",
        (tag_id, item_type, body.id),
    )
    if existing:
        return {"tagged": True, "already_existed": True}

    await db.execute(
        "INSERT INTO user_tag_items (tag_id, item_type, item_id) VALUES (?, ?, ?)",
        (tag_id, item_type, body.id),
    )
    await db.commit()
    return {"tagged": True, "already_existed": False}


@router.delete("/{tag_id}/items/{item_type}/{item_id}")
async def remove_item_from_tag(tag_id: int, item_type: str, item_id: int) -> dict:
    """Remove an item from a tag."""
    db = _db()
    _validate_item_type(item_type)
    result = await db.execute(
        "DELETE FROM user_tag_items WHERE tag_id = ? AND item_type = ? AND item_id = ?",
        (tag_id, item_type, item_id),
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="tag_item_not_found")
    return {"removed": True}


@router.get("/{tag_id}/items")
async def list_tag_items(tag_id: int, item_type: str | None = None) -> dict:
    """List all items in a tag, optionally filtered by type.

    Returns enriched items (track/album/artist details) grouped by type.
    """
    db = _db()
    await _get_tag_or_404(tag_id)

    if item_type:
        _validate_item_type(item_type)

    result: dict[str, list] = {"tracks": [], "albums": [], "artists": []}

    # Fetch tagged tracks
    if item_type is None or item_type == "track":
        rows = await db.fetchall(
            """
            SELECT t.*, uti.created_at as tagged_at
            FROM user_tag_items uti
            JOIN tracks t ON t.id = uti.item_id
            WHERE uti.tag_id = ? AND uti.item_type = 'track'
            ORDER BY uti.created_at DESC
            """,
            (tag_id,),
        )
        result["tracks"] = [dict(r) for r in rows]

    # Fetch tagged albums
    if item_type is None or item_type == "album":
        rows = await db.fetchall(
            """
            SELECT a.*, uti.created_at as tagged_at
            FROM user_tag_items uti
            JOIN albums a ON a.id = uti.item_id
            WHERE uti.tag_id = ? AND uti.item_type = 'album'
            ORDER BY uti.created_at DESC
            """,
            (tag_id,),
        )
        result["albums"] = [dict(r) for r in rows]

    # Fetch tagged artists
    if item_type is None or item_type == "artist":
        rows = await db.fetchall(
            """
            SELECT ar.*, uti.created_at as tagged_at
            FROM user_tag_items uti
            JOIN artists ar ON ar.id = uti.item_id
            WHERE uti.tag_id = ? AND uti.item_type = 'artist'
            ORDER BY uti.created_at DESC
            """,
            (tag_id,),
        )
        result["artists"] = [dict(r) for r in rows]

    return result


# ---------------------------------------------------------------------------
# Library-scoped tag lookups (GET /library/tracks/{id}/tags, etc.)
# ---------------------------------------------------------------------------

async def _get_tags_for_item(item_type: str, item_id: int) -> list[dict]:
    """Return all tags attached to a given library item."""
    db = _db()
    rows = await db.fetchall(
        """
        SELECT ut.*, uti.created_at as tagged_at
        FROM user_tag_items uti
        JOIN user_tags ut ON ut.id = uti.tag_id
        WHERE uti.item_type = ? AND uti.item_id = ?
        ORDER BY ut.name
        """,
        (item_type, item_id),
    )
    return [dict(r) for r in rows]


@library_router.get("/tracks/{track_id}/tags")
async def get_track_tags(track_id: int) -> list[dict]:
    """Get all tags for a track."""
    db = _db()
    item = await db.fetchone("SELECT id FROM tracks WHERE id = ?", (track_id,))
    if not item:
        raise HTTPException(status_code=404, detail="track_not_found")
    return await _get_tags_for_item("track", track_id)


@library_router.get("/albums/{album_id}/tags")
async def get_album_tags(album_id: int) -> list[dict]:
    """Get all tags for an album."""
    db = _db()
    item = await db.fetchone("SELECT id FROM albums WHERE id = ?", (album_id,))
    if not item:
        raise HTTPException(status_code=404, detail="album_not_found")
    return await _get_tags_for_item("album", album_id)


@library_router.get("/artists/{artist_id}/tags")
async def get_artist_tags(artist_id: int) -> list[dict]:
    """Get all tags for an artist."""
    db = _db()
    item = await db.fetchone("SELECT id FROM artists WHERE id = ?", (artist_id,))
    if not item:
        raise HTTPException(status_code=404, detail="artist_not_found")
    return await _get_tags_for_item("artist", artist_id)

"""Metadata Manager — single track/album/artist metadata read/write/update."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps
from tune_server.metadata_manager.models import (
    TrackMetadataUpdate,
    AlbumMetadataUpdate,
    ArtistMetadataUpdate,
)
from tune_server.metadata_manager.tag_writer import write_tags, read_tags

router = APIRouter(tags=["metadata"])


# ---------------------------------------------------------------------------
# Track edit
# ---------------------------------------------------------------------------

@router.patch("/tracks/{track_id}")
async def update_track_metadata(track_id: int, update: TrackMetadataUpdate):
    """Edit track metadata in DB (does NOT write to file)."""
    fields = update.model_dump(exclude_none=True)
    if not fields:
        return {"ok": True, "updated": 0}

    # Handle custom_tags as JSON
    if "custom_tags" in fields:
        fields["custom_tags"] = json.dumps(fields["custom_tags"])

    # artist_name -> resolve to artist_id (artist_name is not a column in PG)
    if "artist_name" in fields:
        artist_name = fields.pop("artist_name")
        # Find or create artist
        row = await deps.db.fetchone(
            "SELECT id FROM artists WHERE name = ?", (artist_name,))
        if row:
            fields["artist_id"] = row["id"]
        else:
            await deps.db.execute(
                "INSERT INTO artists (name) VALUES (?)", (artist_name,))
            await deps.db.commit()
            row = await deps.db.fetchone(
                "SELECT id FROM artists WHERE name = ?", (artist_name,))
            if row:
                fields["artist_id"] = row["id"]

    # album_title -> resolve to album_id
    if "album_title" in fields:
        album_title = fields.pop("album_title")
        # Get current track's artist_id for album lookup
        artist_id = fields.get("artist_id")
        if not artist_id:
            tr = await deps.db.fetchone("SELECT artist_id FROM tracks WHERE id = ?", (track_id,))
            artist_id = tr["artist_id"] if tr else None
        row = await deps.db.fetchone(
            "SELECT id FROM albums WHERE title = ? AND artist_id = ?", (album_title, artist_id))
        if row:
            fields["album_id"] = row["id"]
        elif artist_id:
            await deps.db.execute(
                "INSERT INTO albums (title, artist_id, source) VALUES (?, ?, 'local')",
                (album_title, artist_id))
            await deps.db.commit()
            row = await deps.db.fetchone(
                "SELECT id FROM albums WHERE title = ? AND artist_id = ? ORDER BY id DESC LIMIT 1",
                (album_title, artist_id))
            if row:
                fields["album_id"] = row["id"]

    if not fields:
        return {"ok": True, "updated": 0}

    set_clauses = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [track_id]

    await deps.db.execute(
        f"UPDATE tracks SET {set_clauses}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        tuple(values),
    )
    await deps.db.commit()
    return {"ok": True, "updated": len(fields), "fields": list(fields.keys())}


@router.get("/tracks/{track_id}/tags")
async def read_track_tags(track_id: int):
    """Read tags directly from the audio file (not DB)."""
    row = await deps.db.fetchone("SELECT file_path FROM tracks WHERE id = ?", (track_id,))
    if not row or not row["file_path"]:
        raise HTTPException(status_code=404, detail="Track or file not found")
    return await read_tags(row["file_path"])


@router.post("/tracks/{track_id}/write-tags")
async def write_track_tags(track_id: int):
    """Write current DB metadata to the audio file tags."""
    row = await deps.db.fetchone("SELECT * FROM tracks WHERE id = ?", (track_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Track not found")
    if not row["file_path"]:
        raise HTTPException(status_code=400, detail="No file path for this track")

    metadata = {}
    for field in ["title", "artist_name", "genre", "composer", "year",
                   "lyrics", "comment", "isrc", "bpm", "label"]:
        val = row.get(field) if field in row.keys() else None
        if val is not None:
            metadata[field] = val

    # Album title from join
    if row.get("album_id"):
        album = await deps.db.fetchone("SELECT title FROM albums WHERE id = ?", (row["album_id"],))
        if album:
            metadata["album_title"] = album["title"]

    metadata["track_number"] = row["track_number"]
    metadata["disc_number"] = row["disc_number"]

    result = await write_tags(row["file_path"], metadata)
    return result


# ---------------------------------------------------------------------------
# Album edit
# ---------------------------------------------------------------------------

@router.patch("/albums/{album_id}")
async def update_album_metadata(album_id: int, update: AlbumMetadataUpdate):
    """Edit album metadata in DB."""
    fields = update.model_dump(exclude_none=True)
    if not fields:
        return {"ok": True, "updated": 0}

    set_clauses = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [album_id]

    await deps.db.execute(
        f"UPDATE albums SET {set_clauses}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        tuple(values),
    )
    await deps.db.commit()
    return {"ok": True, "updated": len(fields), "fields": list(fields.keys())}


@router.post("/albums/merge")
async def merge_albums(request: dict):
    """Merge multiple album entries into one, keeping the best metadata."""
    album_ids = request.get("album_ids", [])
    if len(album_ids) < 2:
        raise HTTPException(400, "Need at least 2 album IDs to merge")

    # Fetch all albums
    placeholders = ", ".join(["?" for _ in album_ids])
    albums = await deps.db.fetchall(
        f"SELECT * FROM albums WHERE id IN ({placeholders})", tuple(album_ids)
    )
    if len(albums) < 2:
        raise HTTPException(404, "Albums not found")

    # Pick master: the one with most tracks
    track_counts = {}
    for a in albums:
        count = await deps.db.fetchone(
            "SELECT count(*) as c FROM tracks WHERE album_id = ?", (a["id"],)
        )
        track_counts[a["id"]] = count["c"] if count else 0

    master_id = max(track_counts, key=track_counts.get)
    master = next(a for a in albums if a["id"] == master_id)
    others = [a for a in albums if a["id"] != master_id]

    # Merge best metadata into master
    updates = {}
    if not master.get("cover_path"):
        for o in others:
            if o.get("cover_path"):
                updates["cover_path"] = o["cover_path"]
                break
    if not master.get("genre"):
        for o in others:
            if o.get("genre"):
                updates["genre"] = o["genre"]
                break
    if not master.get("year") or master.get("year", 0) == 0:
        for o in others:
            if o.get("year") and o["year"] > 0:
                updates["year"] = o["year"]
                break

    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [master_id]
        await deps.db.execute(f"UPDATE albums SET {sets} WHERE id = ?", tuple(vals))

    # Move all tracks from others to master
    moved = 0
    for o in others:
        result = await deps.db.execute(
            "UPDATE tracks SET album_id = ?, album_title = ? WHERE album_id = ?",
            (master_id, master["title"], o["id"]),
        )
        moved += track_counts.get(o["id"], 0)

        # Delete the now-empty album
        await deps.db.execute("DELETE FROM albums WHERE id = ?", (o["id"],))

    await deps.db.commit()

    final_count = await deps.db.fetchone(
        "SELECT count(*) as c FROM tracks WHERE album_id = ?", (master_id,)
    )

    return {
        "master_id": master_id,
        "merged": len(others),
        "tracks_moved": moved,
        "total_tracks": final_count["c"] if final_count else 0,
    }


@router.post("/albums/{album_id}/write-tags")
async def write_album_tags(album_id: int):
    """Write DB metadata to all track files in an album."""
    album = await deps.db.fetchone("SELECT * FROM albums WHERE id = ?", (album_id,))
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    tracks = await deps.db.fetchall(
        "SELECT id, file_path FROM tracks WHERE album_id = ?", (album_id,))

    results = []
    for t in tracks:
        if not t["file_path"]:
            continue
        # Build metadata from track + album
        track_row = await deps.db.fetchone("SELECT * FROM tracks WHERE id = ?", (t["id"],))
        metadata = {}
        for field in ["title", "artist_name", "genre", "composer", "year",
                       "lyrics", "comment", "isrc", "bpm", "label"]:
            val = track_row.get(field) if field in track_row.keys() else None
            if val is not None:
                metadata[field] = val
        metadata["album_title"] = album["title"]
        metadata["track_number"] = track_row["track_number"]
        metadata["disc_number"] = track_row["disc_number"]

        r = await write_tags(t["file_path"], metadata)
        results.append({"track_id": t["id"], **r})

    ok_count = sum(1 for r in results if r.get("ok"))

    # Also write cover to album folder if available
    cover_written = False
    if album.get("cover_path"):
        import shutil
        from pathlib import Path as _Path
        from tune_server.config import settings as _settings
        cache_dir = _Path(_settings.artwork_cache_dir)
        first_track = await deps.db.fetchone(
            "SELECT file_path FROM tracks WHERE album_id = ? AND source = 'local' AND file_path IS NOT NULL LIMIT 1",
            (album_id,),
        )
        if first_track and first_track["file_path"]:
            folder = _Path(first_track["file_path"]).parent
            source = cache_dir.parent / album["cover_path"]
            target = folder / "cover.jpg"
            if source.exists() and folder.exists():
                try:
                    shutil.copy2(str(source), str(target))
                    cover_written = True
                except Exception:
                    pass

    return {"album_id": album_id, "tracks_processed": len(results), "success": ok_count, "cover_written": cover_written}


# ---------------------------------------------------------------------------
# Artist edit
# ---------------------------------------------------------------------------

@router.patch("/artists/{artist_id}")
async def update_artist_metadata(artist_id: int, update: ArtistMetadataUpdate):
    """Edit artist metadata in DB."""
    fields = update.model_dump(exclude_none=True)
    if not fields:
        return {"ok": True, "updated": 0}

    set_clauses = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [artist_id]

    await deps.db.execute(
        f"UPDATE artists SET {set_clauses}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        tuple(values),
    )
    await deps.db.commit()
    return {"ok": True, "updated": len(fields)}

"""Metadata Manager API routes — edit, batch, write tags, suggestions."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps
from tune_server.metadata_manager.models import (
    TrackMetadataUpdate,
    AlbumMetadataUpdate,
    ArtistMetadataUpdate,
    BatchTrackEditRequest,
    RenameArtistRequest,
    BatchWriteTagsRequest,
)
from tune_server.metadata_manager.tag_writer import write_tags, read_tags

router = APIRouter(prefix="/metadata", tags=["metadata"])


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
    return {"album_id": album_id, "tracks_processed": len(results), "success": ok_count}


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


# ---------------------------------------------------------------------------
# Batch edit
# ---------------------------------------------------------------------------

@router.post("/batch/tracks")
async def batch_edit_tracks(req: BatchTrackEditRequest):
    """Edit the same fields on multiple tracks at once."""
    fields = req.updates.model_dump(exclude_none=True)
    if not fields or not req.track_ids:
        return {"ok": True, "updated": 0}

    if "custom_tags" in fields:
        fields["custom_tags"] = json.dumps(fields["custom_tags"])

    set_clauses = ", ".join(f"{k} = ?" for k in fields)
    placeholders = ", ".join("?" for _ in req.track_ids)
    values = list(fields.values()) + req.track_ids

    await deps.db.execute(
        f"UPDATE tracks SET {set_clauses}, updated_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
        tuple(values),
    )
    await deps.db.commit()
    return {"ok": True, "tracks_updated": len(req.track_ids), "fields": list(fields.keys())}


@router.post("/batch/rename-artist")
async def rename_artist(req: RenameArtistRequest):
    """Rename an artist everywhere: artists table + all tracks."""
    # Update artist table
    result_artist = await deps.db.execute(
        "UPDATE artists SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
        (req.new_name, req.old_name),
    )

    # Update tracks
    result_tracks = await deps.db.execute(
        "UPDATE tracks SET artist_name = ?, updated_at = CURRENT_TIMESTAMP WHERE artist_name = ?",
        (req.new_name, req.old_name),
    )

    # Update albums
    result_albums = await deps.db.execute(
        "UPDATE albums SET artist_name = ?, updated_at = CURRENT_TIMESTAMP WHERE artist_name = ?",
        (req.new_name, req.old_name),
    )

    await deps.db.commit()

    # Optionally write to files
    if req.update_files:
        tracks = await deps.db.fetchall(
            "SELECT id, file_path FROM tracks WHERE artist_name = ?", (req.new_name,))
        for t in tracks:
            if t["file_path"]:
                await write_tags(t["file_path"], {"artist_name": req.new_name})

    return {
        "ok": True,
        "old_name": req.old_name,
        "new_name": req.new_name,
        "files_updated": req.update_files,
    }


@router.post("/batch/write-tags")
async def batch_write_tags(req: BatchWriteTagsRequest):
    """Write DB metadata to files for multiple tracks."""
    results = []
    for tid in req.track_ids:
        try:
            r = await write_track_tags(tid)
            results.append({"track_id": tid, **r})
        except Exception as e:
            results.append({"track_id": tid, "ok": False, "error": str(e)})

    ok_count = sum(1 for r in results if r.get("ok"))
    return {"tracks_processed": len(results), "success": ok_count, "results": results}


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

@router.get("/suggestions")
async def list_suggestions(status: str = "pending", limit: int = 100):
    """List metadata suggestions."""
    rows = await deps.db.fetchall(
        "SELECT * FROM metadata_suggestions WHERE status = ? ORDER BY confidence DESC LIMIT ?",
        (status, limit),
    )
    return [dict(r) for r in rows]


@router.post("/suggestions/{suggestion_id}/accept")
async def accept_suggestion(suggestion_id: int):
    """Accept a suggestion and apply it."""
    row = await deps.db.fetchone(
        "SELECT * FROM metadata_suggestions WHERE id = ?", (suggestion_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    # Apply the change
    if row["track_id"]:
        await deps.db.execute(
            f"UPDATE tracks SET {row['field']} = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (row["suggested_value"], row["track_id"]),
        )
    elif row["album_id"]:
        await deps.db.execute(
            f"UPDATE albums SET {row['field']} = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (row["suggested_value"], row["album_id"]),
        )

    await deps.db.execute(
        "UPDATE metadata_suggestions SET status = 'accepted' WHERE id = ?",
        (suggestion_id,),
    )
    await deps.db.commit()
    return {"ok": True, "field": row["field"], "value": row["suggested_value"]}


@router.post("/suggestions/{suggestion_id}/reject")
async def reject_suggestion(suggestion_id: int):
    """Reject a suggestion."""
    await deps.db.execute(
        "UPDATE metadata_suggestions SET status = 'rejected' WHERE id = ?",
        (suggestion_id,),
    )
    await deps.db.commit()
    return {"ok": True}


@router.post("/suggestions/accept-all")
async def accept_all_suggestions(min_confidence: float = 0.9):
    """Accept all suggestions above a confidence threshold."""
    rows = await deps.db.fetchall(
        "SELECT * FROM metadata_suggestions WHERE status = 'pending' AND confidence >= ?",
        (min_confidence,),
    )
    applied = 0
    for row in rows:
        if row["track_id"]:
            await deps.db.execute(
                f"UPDATE tracks SET {row['field']} = ? WHERE id = ?",
                (row["suggested_value"], row["track_id"]),
            )
        elif row["album_id"]:
            await deps.db.execute(
                f"UPDATE albums SET {row['field']} = ? WHERE id = ?",
                (row["suggested_value"], row["album_id"]),
            )
        await deps.db.execute(
            "UPDATE metadata_suggestions SET status = 'accepted' WHERE id = ?",
            (row["id"],),
        )
        applied += 1

    await deps.db.commit()
    return {"ok": True, "applied": applied}

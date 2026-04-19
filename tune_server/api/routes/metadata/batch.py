"""Metadata Manager — batch edit operations, write-all, auto-fix albums."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps
from tune_server.metadata_manager.models import (
    BatchTrackEditRequest,
    RenameArtistRequest,
    BatchWriteTagsRequest,
)
from tune_server.metadata_manager.tag_writer import write_tags

router = APIRouter(tags=["metadata"])


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
    from tune_server.api.routes.metadata.track_edits import write_track_tags

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
# Write all metadata to files + covers to folders
# ---------------------------------------------------------------------------

@router.post("/write-all-tags")
async def write_all_tags_to_files():
    """Write DB metadata (genre, year, artist, disc_number) to all local files."""
    rows = await deps.db.fetchall(
        """SELECT t.id, t.title, t.artist_name, t.track_number, t.disc_number,
                  t.file_path, t.format,
                  a.title as album_title, a.genre, a.year
           FROM tracks t
           JOIN albums a ON t.album_id = a.id
           WHERE t.source = 'local' AND t.file_path IS NOT NULL
           ORDER BY t.file_path""",
    )

    total = len(rows)
    updated = 0
    skipped = 0
    errors = 0

    for row in rows:
        fmt = (row["format"] or "").lower()
        if fmt in ("wav",):
            skipped += 1
            continue

        metadata = {}
        if row["artist_name"]:
            metadata["artist_name"] = row["artist_name"]
        if row["album_title"]:
            metadata["album_title"] = row["album_title"]
        if row["title"]:
            metadata["title"] = row["title"]
        if row["genre"]:
            metadata["genre"] = row["genre"]
        if row["year"] and row["year"] > 0:
            metadata["year"] = str(row["year"])
        if row["track_number"] and row["track_number"] > 0:
            metadata["track_number"] = str(row["track_number"])
        if row["disc_number"] and row["disc_number"] > 0:
            metadata["disc_number"] = str(row["disc_number"])

        if not metadata:
            skipped += 1
            continue

        try:
            result = await write_tags(row["file_path"], metadata)
            if result.get("ok"):
                updated += 1
            else:
                errors += 1
        except Exception:
            errors += 1

    return {"ok": True, "total": total, "updated": updated, "skipped": skipped, "errors": errors}


@router.post("/write-all-covers")
async def write_all_covers_to_folders():
    """Copy cover.jpg from artwork_cache to each album folder."""
    import shutil
    from pathlib import Path
    from tune_server.config import settings

    cache_dir = Path(settings.artwork_cache_dir)

    rows = await deps.db.fetchall(
        """SELECT DISTINCT a.id, a.cover_path,
                  (SELECT regexp_replace(t.file_path, '/[^/]+$', '')
                   FROM tracks t WHERE t.album_id = a.id AND t.source = 'local' LIMIT 1) as folder
           FROM albums a
           JOIN tracks t ON t.album_id = a.id
           WHERE a.cover_path IS NOT NULL AND a.cover_path != ''
           AND t.source = 'local'""",
    )

    written = 0
    skipped = 0
    errors = 0

    for row in rows:
        folder = row["folder"]
        cover = row["cover_path"]
        if not folder or not cover:
            skipped += 1
            continue

        target = Path(folder) / "cover.jpg"
        if target.exists():
            skipped += 1
            continue

        source = cache_dir.parent / cover  # cover_path is relative like "artwork_cache/xxx.jpg"
        if not source.exists():
            errors += 1
            continue

        try:
            shutil.copy2(str(source), str(target))
            written += 1
        except Exception:
            errors += 1

    return {"ok": True, "written": written, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Auto-fix albums from file paths
# ---------------------------------------------------------------------------

@router.post("/auto-fix-albums")
async def auto_fix_albums_from_paths():
    """Auto-detect album names from file paths for tracks in 'Unknown Album'.

    Parses path pattern: .../Artist/AlbumName/track.flac
    Creates or finds albums and reassigns tracks.
    """
    rows = await deps.db.fetchall(
        """SELECT t.id, t.title, t.file_path, t.artist_id, ar.name as artist_name,
                  a.id as album_id, a.title as album_title
           FROM tracks t
           LEFT JOIN artists ar ON ar.id = t.artist_id
           LEFT JOIN albums a ON a.id = t.album_id
           WHERE (a.title = 'Unknown Album' OR a.title IS NULL)
             AND t.file_path IS NOT NULL""",
    )

    fixed = 0
    created_albums = {}

    for row in rows:
        path = row["file_path"]
        if not path:
            continue

        # Extract album from path: .../Artist/AlbumName/track.ext
        parts = path.replace("\\", "/").split("/")
        if len(parts) < 3:
            continue

        album_name = parts[-2]  # Parent folder = album name
        artist_name = row["artist_name"] or parts[-3] if len(parts) >= 3 else ""

        # Skip if album name is still "Unknown Album" or too short
        if album_name.lower() in ("unknown album", "unknown", ""):
            continue

        # Find or create album
        cache_key = f"{artist_name}|||{album_name}"
        if cache_key in created_albums:
            new_album_id = created_albums[cache_key]
        else:
            # Check if album already exists
            existing = await deps.db.fetchone(
                "SELECT id FROM albums WHERE title = ? AND artist_id = ?",
                (album_name, row["artist_id"]),
            )
            if existing:
                new_album_id = existing["id"]
            else:
                # Create new album
                await deps.db.execute(
                    "INSERT INTO albums (title, artist_id, source) VALUES (?, ?, 'local')",
                    (album_name, row["artist_id"]),
                )
                await deps.db.commit()
                new_row = await deps.db.fetchone(
                    "SELECT id FROM albums WHERE title = ? AND artist_id = ? ORDER BY id DESC LIMIT 1",
                    (album_name, row["artist_id"]),
                )
                new_album_id = new_row["id"] if new_row else None

            if new_album_id:
                created_albums[cache_key] = new_album_id

        # Reassign track to correct album
        if new_album_id and new_album_id != row["album_id"]:
            await deps.db.execute(
                "UPDATE tracks SET album_id = ? WHERE id = ?",
                (new_album_id, row["id"]),
            )
            fixed += 1

    await deps.db.commit()

    # Cleanup: delete "Unknown Album" if empty
    await deps.db.execute(
        """DELETE FROM albums WHERE title = 'Unknown Album'
           AND id NOT IN (SELECT DISTINCT album_id FROM tracks WHERE album_id IS NOT NULL)""",
    )
    await deps.db.commit()

    return {
        "ok": True,
        "tracks_fixed": fixed,
        "albums_created": len(created_albums),
        "total_unknown": len(rows),
    }

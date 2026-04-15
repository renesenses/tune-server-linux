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
from tune_server.metadata_manager.matcher import lookup_track, lookup_album
from tune_server.metadata_manager.enricher import enrich_track, enrich_album
from tune_server.metadata_manager.cover_fetcher import search_covers

router = APIRouter(prefix="/metadata", tags=["metadata"])

ALLOWED_METADATA_COLUMNS = {'title', 'artist_name', 'album_title', 'genre', 'year', 'track_number', 'disc_number'}


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

    # artist_name → resolve to artist_id (artist_name is not a column in PG)
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

    # album_title → resolve to album_id
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
    if row['field'] not in ALLOWED_METADATA_COLUMNS:
        raise HTTPException(status_code=400, detail=f"Invalid metadata field: {row['field']}")

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
        if row['field'] not in ALLOWED_METADATA_COLUMNS:
            continue  # skip invalid column
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


# ---------------------------------------------------------------------------
# Lookup (MusicBrainz)
# ---------------------------------------------------------------------------

@router.post("/lookup")
async def lookup_track_endpoint(title: str, artist: str = "", album: str = ""):
    """Search MusicBrainz for a track. Returns candidates."""
    results = await lookup_track(title, artist, album)
    return {"results": results}


@router.post("/lookup-album")
async def lookup_album_endpoint(title: str, artist: str = ""):
    """Search MusicBrainz for an album. Returns candidates."""
    results = await lookup_album(title, artist)
    return {"results": results}


# ---------------------------------------------------------------------------
# Enrichment (multi-source)
# ---------------------------------------------------------------------------

@router.post("/enrich/{track_id}")
async def enrich_track_endpoint(track_id: int):
    """Enrich a track from MusicBrainz + Last.fm. Creates suggestions."""
    row = await deps.db.fetchone("SELECT * FROM tracks WHERE id = ?", (track_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Track not found")

    album_row = await deps.db.fetchone("SELECT title FROM albums WHERE id = ?", (row["album_id"],)) if row.get("album_id") else None

    from tune_server.config import settings
    result = await enrich_track(
        track_id=track_id,
        title=row["title"],
        artist=row.get("artist_name") or "",
        album=album_row["title"] if album_row else "",
        db=deps.db,
        lastfm_key=getattr(settings, "lastfm_api_key", ""),
        discogs_token=getattr(settings, "discogs_token", ""),
        cache_dir=getattr(settings, "artwork_cache_dir", "artwork_cache"),
    )
    return result


@router.post("/enrich-album/{album_id}")
async def enrich_album_endpoint(album_id: int):
    """Enrich an album from MusicBrainz + fetch cover art."""
    row = await deps.db.fetchone("SELECT * FROM albums WHERE id = ?", (album_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Album not found")

    from tune_server.config import settings
    result = await enrich_album(
        album_id=album_id,
        title=row["title"],
        artist=row.get("artist_name") or "",
        db=deps.db,
        discogs_token=getattr(settings, "discogs_token", ""),
        cache_dir=getattr(settings, "artwork_cache_dir", "artwork_cache"),
    )
    return result


# ---------------------------------------------------------------------------
# Covers
# ---------------------------------------------------------------------------

@router.get("/covers/search")
async def search_covers_endpoint(album: str, artist: str = "", release_id: str = ""):
    """Search for album covers from Cover Art Archive + Discogs."""
    from tune_server.config import settings
    results = await search_covers(
        album_title=album,
        artist_name=artist,
        musicbrainz_release_id=release_id,
        discogs_token=getattr(settings, "discogs_token", ""),
        cache_dir=getattr(settings, "artwork_cache_dir", "artwork_cache"),
    )
    return {"results": results}


@router.post("/covers/album/{album_id}")
async def fetch_album_cover(album_id: int):
    """Auto-fetch and assign a cover to an album."""
    row = await deps.db.fetchone("SELECT * FROM albums WHERE id = ?", (album_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Album not found")

    from tune_server.config import settings
    mb_id = row.get("musicbrainz_release_id") or "" if "musicbrainz_release_id" in row.keys() else ""

    results = await search_covers(
        album_title=row["title"],
        artist_name=row.get("artist_name") or "",
        musicbrainz_release_id=mb_id,
        discogs_token=getattr(settings, "discogs_token", ""),
        cache_dir=getattr(settings, "artwork_cache_dir", "artwork_cache"),
    )

    if results:
        cover_path = results[0]["local_path"]
        await deps.db.execute(
            "UPDATE albums SET cover_path = ? WHERE id = ?",
            (cover_path, album_id),
        )
        await deps.db.commit()
        return {"ok": True, "cover_path": cover_path, "source": results[0]["source"]}

    return {"ok": False, "error": "No cover found"}


# ---------------------------------------------------------------------------
# Fingerprinting (AcoustID/Chromaprint)
# ---------------------------------------------------------------------------

@router.post("/fingerprint/{track_id}")
async def fingerprint_track(track_id: int):
    """Identify a track by its audio fingerprint."""
    from tune_server.metadata_manager.fingerprint import identify_track

    row = await deps.db.fetchone("SELECT file_path FROM tracks WHERE id = ?", (track_id,))
    if not row or not row["file_path"]:
        raise HTTPException(status_code=404, detail="Track or file not found")

    result = await identify_track(row["file_path"])
    if not result:
        return {"identified": False, "track_id": track_id}

    return {"identified": True, "track_id": track_id, **result}


@router.post("/fingerprint-batch")
async def fingerprint_batch(track_ids: list[int] | None = None, limit: int = 50):
    """Batch fingerprint identification. If no IDs given, scans unidentified tracks."""
    from tune_server.metadata_manager.fingerprint import identify_batch

    if track_ids:
        rows = await deps.db.fetchall(
            f"SELECT id, file_path, title, artist_name FROM tracks WHERE id IN ({','.join('?' * len(track_ids))})",
            tuple(track_ids),
        )
    else:
        rows = await deps.db.fetchall(
            """SELECT id, file_path, title, artist_name FROM tracks
               WHERE source = 'local' AND (acoustid IS NULL OR acoustid = '')
               AND file_path IS NOT NULL
               LIMIT ?""",
            (limit,),
        )

    tracks = [dict(r) for r in rows]
    result = await identify_batch(tracks, db=deps.db)
    return result


# ---------------------------------------------------------------------------
# Auto-fix background scan
# ---------------------------------------------------------------------------

@router.post("/auto-fix")
async def start_auto_fix():
    """Start a background auto-fix scan of the library."""
    from tune_server.metadata_manager.auto_fix import get_auto_fix_engine
    from tune_server.config import settings

    engine = get_auto_fix_engine()
    if engine.status["status"] == "running":
        return {"ok": False, "error": "Scan already in progress", "status": engine.status}

    await engine.start_scan(
        db=deps.db,
        event_bus=deps.event_bus,
        lastfm_key=getattr(settings, "lastfm_api_key", ""),
        discogs_token=getattr(settings, "discogs_token", ""),
        cache_dir=getattr(settings, "artwork_cache_dir", "artwork_cache"),
    )
    return {"ok": True, "status": "started"}


@router.get("/auto-fix/status")
async def auto_fix_status():
    """Get status of the current auto-fix scan."""
    from tune_server.metadata_manager.auto_fix import get_auto_fix_engine
    return get_auto_fix_engine().status


@router.get("/auto-fix/report")
async def auto_fix_report(limit: int = 10):
    """Get the last N auto-fix reports."""
    rows = await deps.db.fetchall(
        "SELECT * FROM metadata_fix_reports ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Duplicates (audio hash MD5)
# ---------------------------------------------------------------------------

@router.post("/duplicates/scan")
async def scan_duplicates_endpoint(limit: int = 5000):
    """Scan library for duplicate tracks by audio content hash."""
    from tune_server.metadata_manager.duplicate import scan_duplicates
    result = await scan_duplicates(deps.db, limit=limit)
    return result


@router.get("/duplicates")
async def list_duplicates():
    """List detected duplicate groups with full metadata for comparison."""
    rows = await deps.db.fetchall(
        """SELECT d.id, d.audio_hash, d.resolved,
                  ta.id as a_id, ta.title as a_title, ta.artist_name as a_artist,
                  ta.file_path as a_path, ta.format as a_format,
                  ta.sample_rate as a_sr, ta.bit_depth as a_bd,
                  ta.genre as a_genre, ta.year as a_year,
                  ta.album_title as a_album,
                  tb.id as b_id, tb.title as b_title, tb.artist_name as b_artist,
                  tb.file_path as b_path, tb.format as b_format,
                  tb.sample_rate as b_sr, tb.bit_depth as b_bd,
                  tb.genre as b_genre, tb.year as b_year,
                  tb.album_title as b_album
           FROM duplicate_tracks d
           JOIN tracks ta ON ta.id = d.track_id_a
           JOIN tracks tb ON tb.id = d.track_id_b
           WHERE d.resolved = false
           ORDER BY d.audio_hash""",
    )
    result = []
    for r in rows:
        # Compute file sizes
        import os
        a_size = 0
        b_size = 0
        try:
            a_size = os.path.getsize(r["a_path"]) if r["a_path"] and os.path.exists(r["a_path"]) else 0
        except OSError:
            pass
        try:
            b_size = os.path.getsize(r["b_path"]) if r["b_path"] and os.path.exists(r["b_path"]) else 0
        except OSError:
            pass

        # Flag metadata differences
        diffs = []
        if (r["a_artist"] or "") != (r["b_artist"] or ""):
            diffs.append("artist")
        if (r["a_genre"] or "") != (r["b_genre"] or ""):
            diffs.append("genre")
        if (r.get("a_year") or 0) != (r.get("b_year") or 0):
            diffs.append("year")
        if (r["a_title"] or "") != (r["b_title"] or ""):
            diffs.append("title")
        if (r["a_album"] or "") != (r["b_album"] or ""):
            diffs.append("album")

        result.append({
            "id": r["id"],
            "audio_hash": r["audio_hash"],
            "differences": diffs,
            "a": {
                "track_id": r["a_id"], "title": r["a_title"], "artist": r["a_artist"],
                "album": r["a_album"], "genre": r["a_genre"], "year": r.get("a_year"),
                "path": r["a_path"], "format": r["a_format"],
                "sample_rate": r["a_sr"], "bit_depth": r["a_bd"], "size": a_size,
            },
            "b": {
                "track_id": r["b_id"], "title": r["b_title"], "artist": r["b_artist"],
                "album": r["b_album"], "genre": r["b_genre"], "year": r.get("b_year"),
                "path": r["b_path"], "format": r["b_format"],
                "sample_rate": r["b_sr"], "bit_depth": r["b_bd"], "size": b_size,
            },
        })

    # Group by album pairs to detect full album duplicates
    album_pairs: dict[tuple, list] = {}
    for d in result:
        key = tuple(sorted([d["a"].get("album") or "", d["b"].get("album") or ""]))
        if key not in album_pairs:
            album_pairs[key] = []
        album_pairs[key].append(d)

    # Annotate each duplicate with type: "track" or "album"
    for d in result:
        key = tuple(sorted([d["a"].get("album") or "", d["b"].get("album") or ""]))
        group = album_pairs[key]
        if len(group) >= 3:
            d["type"] = "album"
            d["album_duplicate_count"] = len(group)
        else:
            d["type"] = "track"
            d["album_duplicate_count"] = 1

    return result


@router.post("/duplicates/resolve")
async def resolve_duplicate(duplicate_id: int, keep_track_id: int):
    """Resolve a duplicate — keep one track, move the other to /data/duplicates/."""
    import shutil
    from pathlib import Path
    from tune_server.config import settings

    row = await deps.db.fetchone(
        "SELECT * FROM duplicate_tracks WHERE id = ?", (duplicate_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Duplicate not found")

    # Determine which to remove
    remove_id = row["track_id_b"] if keep_track_id == row["track_id_a"] else row["track_id_a"]

    # Get the file path of the track to remove
    remove_track = await deps.db.fetchone("SELECT file_path FROM tracks WHERE id = ?", (remove_id,))

    moved = False
    if remove_track and remove_track["file_path"]:
        src = Path(remove_track["file_path"])
        if src.exists():
            # Move to duplicates dir, preserving relative path
            dup_dir = Path(settings.duplicates_dir)
            dup_dir.mkdir(parents=True, exist_ok=True)
            dest = dup_dir / src.name
            # Avoid name collision
            if dest.exists():
                dest = dup_dir / f"{src.stem}_{remove_id}{src.suffix}"
            shutil.move(str(src), str(dest))
            moved = True

    # Remove from database
    await deps.db.execute("DELETE FROM tracks WHERE id = ?", (remove_id,))

    # Mark as resolved
    await deps.db.execute(
        "UPDATE duplicate_tracks SET resolved = true WHERE id = ?", (duplicate_id,))
    await deps.db.commit()

    return {
        "ok": True,
        "kept": keep_track_id,
        "removed": remove_id,
        "moved": moved,
        "destination": str(Path(settings.duplicates_dir)) if moved else None,
    }


@router.post("/duplicates/move-album")
async def move_album_to_duplicates(album_id: int):
    """Move all tracks of an album to /data/duplicates/ and remove from library."""
    import shutil
    from pathlib import Path
    from tune_server.config import settings

    # Get album
    album = await deps.db.fetchone("SELECT * FROM albums WHERE id = ?", (album_id,))
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    # Get all tracks
    tracks = await deps.db.fetchall("SELECT id, file_path FROM tracks WHERE album_id = ?", (album_id,))

    dup_dir = Path(settings.duplicates_dir)
    try:
        dup_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fallback: try under /opt/tune-server/duplicates
        dup_dir = Path("/opt/tune-server/duplicates")
        dup_dir.mkdir(parents=True, exist_ok=True)

    moved_count = 0
    for t in tracks:
        src = Path(t["file_path"]) if t["file_path"] else None
        if src and src.exists():
            # Preserve album folder structure in duplicates dir
            album_folder = src.parent.name
            dest_dir = dup_dir / album_folder
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            if dest.exists():
                dest = dest_dir / f"{src.stem}_{t['id']}{src.suffix}"
            try:
                shutil.move(str(src), str(dest))
                moved_count += 1
            except Exception as e:
                logger.warning("move_duplicate_error", track_id=t["id"], error=str(e))

    # Remove tracks and album from database
    await deps.db.execute("DELETE FROM tracks WHERE album_id = ?", (album_id,))
    await deps.db.execute("DELETE FROM albums WHERE id = ?", (album_id,))
    await deps.db.commit()

    return {
        "ok": True,
        "album_id": album_id,
        "tracks_moved": moved_count,
        "total_tracks": len(tracks),
        "destination": str(dup_dir),
    }


# ---------------------------------------------------------------------------
# Cover upload
# ---------------------------------------------------------------------------

@router.post("/covers/album/{album_id}/upload")
async def upload_album_cover(album_id: int):
    """Upload a cover image for an album (multipart form)."""
    from fastapi import UploadFile, File as FastAPIFile
    # This endpoint needs to be called with multipart/form-data
    # For now, provide the basic structure
    return {"error": "Use PUT /library/albums/{id}/artwork with multipart upload"}


@router.post("/covers/track/{track_id}/embed")
async def embed_cover_in_file(track_id: int):
    """Embed the album cover into the track's audio file tags."""
    import mutagen
    from mutagen.id3 import APIC
    from pathlib import Path

    row = await deps.db.fetchone(
        """SELECT t.file_path, a.cover_path
           FROM tracks t
           LEFT JOIN albums a ON a.id = t.album_id
           WHERE t.id = ?""",
        (track_id,),
    )
    if not row or not row["file_path"]:
        raise HTTPException(status_code=404, detail="Track not found")
    if not row["cover_path"]:
        return {"ok": False, "error": "No cover art available for this album"}

    cover_path = Path(row["cover_path"])
    if not cover_path.is_absolute():
        from tune_server.config import settings
        cover_path = Path(settings.artwork_cache_dir) / cover_path.name

    if not cover_path.exists():
        return {"ok": False, "error": f"Cover file not found: {cover_path}"}

    try:
        audio = mutagen.File(row["file_path"])
        if audio is None:
            return {"ok": False, "error": "Unsupported audio format"}

        cover_data = cover_path.read_bytes()
        mime = "image/jpeg" if cover_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"

        # ID3 (MP3)
        if hasattr(audio, 'tags') and hasattr(audio.tags, 'add'):
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(APIC(
                encoding=3, mime=mime, type=3,
                desc="Front", data=cover_data,
            ))
            audio.save()
            return {"ok": True, "embedded": True, "size": len(cover_data)}

        # FLAC
        if hasattr(audio, 'pictures'):
            from mutagen.flac import Picture
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.data = cover_data
            audio.clear_pictures()
            audio.add_picture(pic)
            audio.save()
            return {"ok": True, "embedded": True, "size": len(cover_data)}

        return {"ok": False, "error": "Cover embedding not supported for this format"}

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Doubtful metadata — albums with inferred/uncertain data
# ---------------------------------------------------------------------------

@router.get("/doubtful")
async def get_doubtful_albums():
    """Return albums with doubtful metadata (inferred from paths, propagated, etc.)."""
    rows = await deps.db.fetchall(
        """SELECT al.id, al.title, al.artist_name, al.genre, al.year,
                  al.cover_path, al.source, ar.name as artist_resolved
           FROM albums al
           LEFT JOIN artists ar ON al.artist_id = ar.id
           WHERE
             -- Artist is ALL CAPS and > 3 chars (inferred from folder name)
             (al.artist_name = UPPER(al.artist_name) AND LENGTH(al.artist_name) > 4
              AND al.artist_name NOT SIMILAR TO '[A-Z]{2,4}'
              AND LOWER(al.artist_name) NOT IN ('various artists', 'various', 'compilation',
                  'compilations', 'multi-artistes', 'multi artistes',
                  'various artists & performers'))
             -- Artist is a placeholder
             OR LOWER(al.artist_name) IN ('inconnu', 'none')
             -- Genre is a placeholder
             OR LOWER(al.genre) IN ('other', 'divers')
             -- Year seems wrong (before 1920 or after current year)
             OR (al.year IS NOT NULL AND al.year > 0 AND (al.year < 1920 OR al.year > 2026))
             -- Album title looks like an ALL CAPS path component
             OR (al.title = UPPER(al.title) AND LENGTH(al.title) > 4
                 AND al.title NOT SIMILAR TO '[A-Z]{2,4}')
             -- Artist name contains year prefix (folder-derived like "1970-The Complete...")
             OR al.artist_name ~ '^\d{4}[-\s]'
           ORDER BY al.artist_name, al.title""",
    )
    results = []
    for r in rows:
        reasons = _doubtful_reasons(r)
        if not reasons:
            continue
        results.append({
            "id": r["id"],
            "title": r["title"],
            "artist_name": r["artist_name"],
            "artist_resolved": r["artist_resolved"],
            "genre": r["genre"],
            "year": r["year"],
            "cover_path": r["cover_path"],
            "source": r["source"],
            "reasons": reasons,
        })
    return results


def _doubtful_reasons(row) -> list[str]:
    """Determine why an album is flagged as doubtful."""
    import re
    reasons = []
    artist = row.get("artist_name") or ""
    genre = row.get("genre") or ""
    year = row.get("year") or 0
    title = row.get("title") or ""

    _compilation_artists = {'various artists', 'various', 'compilation', 'compilations',
                             'multi-artistes', 'multi artistes', 'various artists & performers'}
    if artist and artist == artist.upper() and len(artist) > 4 and artist.lower() not in _compilation_artists:
        reasons.append("artist_uppercase")
    if artist.lower() in ('inconnu', 'none'):
        reasons.append("artist_placeholder")
    if re.match(r'^\d{4}[-\s]', artist):
        reasons.append("artist_has_year")
    if genre.lower() in ('other', 'divers'):
        reasons.append("genre_placeholder")
    if year and (year < 1920 or year > 2026):
        reasons.append("year_suspicious")
    if title and len(title) > 4 and title == title.upper():
        reasons.append("title_uppercase")
    return reasons


# ---------------------------------------------------------------------------
# Write all metadata to files + covers to folders
# ---------------------------------------------------------------------------

@router.post("/write-all-tags")
async def write_all_tags_to_files():
    """Write DB metadata (genre, year, artist, disc_number) to all local files."""
    import asyncio as _aio

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

    import os
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


# ---------------------------------------------------------------------------
# Fix missing years from Tidal
# ---------------------------------------------------------------------------

@router.post("/fix-years-tidal")
async def fix_years_from_tidal():
    """Fill missing album years by searching Tidal.

    For each album with year IS NULL or 0, search Tidal by title + artist.
    If a match is found, update the year in the local database.
    """
    import asyncio
    from difflib import SequenceMatcher

    tidal = deps.streaming_services.get("tidal")
    if not tidal:
        raise HTTPException(status_code=400, detail="Tidal not connected")

    # Get all albums missing year
    rows = await deps.db.fetchall(
        """SELECT al.id, al.title, ar.name as artist_name
           FROM albums al
           LEFT JOIN artists ar ON al.artist_id = ar.id
           WHERE (al.year IS NULL OR al.year = 0)
           ORDER BY al.title""",
    )

    if not rows:
        return {"ok": True, "total": 0, "fixed": 0, "not_found": 0}

    fixed = 0
    not_found = 0
    results = []
    missing = []

    def _normalize(s: str) -> str:
        import unicodedata
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        s = s.lower().strip()
        if s.startswith("the "):
            s = s[4:]
        # Remove quality suffixes like (96kHz/24bit)
        import re
        s = re.sub(r"\s*\(\d+k?hz[^)]*\)", "", s, flags=re.IGNORECASE)
        return s

    for row in rows:
        album_title = row["title"]
        artist_name = row["artist_name"] or ""
        album_id = row["id"]

        if not album_title or album_title == "Unknown Album":
            not_found += 1
            continue

        query = f"{artist_name} {album_title}".strip()
        try:
            search_result = await tidal.search(query, limit=10)
        except Exception:
            not_found += 1
            continue

        # Find best matching album
        best_year = None
        best_score = 0.0

        norm_title = _normalize(album_title)
        norm_artist = _normalize(artist_name)

        for album in search_result.albums:
            t_score = SequenceMatcher(None, norm_title, _normalize(album.title)).ratio()
            a_score = SequenceMatcher(None, norm_artist, _normalize(album.artist_name)).ratio() if norm_artist else 1.0
            combined = t_score * 0.7 + a_score * 0.3

            if combined > best_score and album.year and album.year > 1900:
                best_score = combined
                best_year = album.year

        if best_year and best_score >= 0.6:
            await deps.db.execute(
                "UPDATE albums SET year = ? WHERE id = ?",
                (best_year, album_id),
            )
            fixed += 1
            results.append({"album": album_title, "artist": artist_name, "year": best_year, "score": round(best_score, 2)})
        else:
            not_found += 1
            missing.append({"id": album_id, "album": album_title, "artist": artist_name})

        # Rate limit: avoid hammering Tidal
        await asyncio.sleep(0.3)

    await deps.db.commit()

    return {
        "ok": True,
        "total": len(rows),
        "fixed": fixed,
        "not_found": not_found,
        "details": results,
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# Fix missing years from file tags
# ---------------------------------------------------------------------------

@router.post("/fix-years-tags")
async def fix_years_from_file_tags():
    """Fill missing album years by reading the DATE/YEAR tag from audio files.

    For each album without year, pick a track, read its tags with mutagen,
    and extract the year.
    """
    from pathlib import Path

    rows = await deps.db.fetchall(
        """SELECT al.id, al.title, t.file_path
           FROM albums al
           JOIN tracks t ON t.album_id = al.id
           WHERE (al.year IS NULL OR al.year = 0)
             AND t.file_path IS NOT NULL
           ORDER BY al.id""",
    )

    if not rows:
        return {"ok": True, "total": 0, "fixed": 0}

    # Group by album (take first track per album)
    albums: dict[int, dict] = {}
    for r in rows:
        aid = r["id"]
        if aid not in albums:
            albums[aid] = {"title": r["title"], "file_path": r["file_path"]}

    fixed = 0
    results = []
    tag_keys = ["date", "TDRC", "TYER", "\xa9day", "year", "DATE", "YEAR"]

    for album_id, info in albums.items():
        fp = Path(info["file_path"])
        if not fp.exists():
            continue

        try:
            import mutagen
            audio = mutagen.File(str(fp), easy=True)
            if not audio or not audio.tags:
                continue

            year = None
            for key in tag_keys:
                val = audio.tags.get(key)
                if val:
                    raw = str(val[0]) if isinstance(val, list) else str(val)
                    raw = raw.strip()[:4]
                    if raw.isdigit() and 1900 < int(raw) < 2030:
                        year = int(raw)
                        break

            if not year:
                # Try non-easy mode
                audio2 = mutagen.File(str(fp))
                if audio2 and audio2.tags:
                    for key in tag_keys:
                        val = audio2.tags.get(key)
                        if val:
                            raw = str(val.text[0]) if hasattr(val, "text") else str(val[0]) if isinstance(val, list) else str(val)
                            raw = raw.strip()[:4]
                            if raw.isdigit() and 1900 < int(raw) < 2030:
                                year = int(raw)
                                break

            if year:
                await deps.db.execute(
                    "UPDATE albums SET year = ? WHERE id = ?", (year, album_id))
                fixed += 1
                results.append({"album": info["title"], "year": year})
        except Exception:
            continue

    await deps.db.commit()

    return {
        "ok": True,
        "total": len(albums),
        "fixed": fixed,
        "not_found": len(albums) - fixed,
        "details": results,
    }


# ---------------------------------------------------------------------------
# Fix missing years from Last.fm
# ---------------------------------------------------------------------------

@router.post("/fix-years-lastfm")
async def fix_years_from_lastfm():
    """Fill missing album years by querying Last.fm API."""
    import asyncio
    import aiohttp
    from tune_server.config import settings

    api_key = getattr(settings, "lastfm_api_key", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="Last.fm API key not configured")

    rows = await deps.db.fetchall(
        """SELECT al.id, al.title, ar.name as artist_name
           FROM albums al
           LEFT JOIN artists ar ON al.artist_id = ar.id
           WHERE (al.year IS NULL OR al.year = 0)
           ORDER BY al.title""",
    )

    if not rows:
        return {"ok": True, "total": 0, "fixed": 0}

    fixed = 0
    results = []

    async with aiohttp.ClientSession() as session:
        for row in rows:
            album_title = row["title"]
            artist_name = row["artist_name"] or ""
            album_id = row["id"]

            if not album_title or album_title in ("Unknown Album", ""):
                continue

            try:
                params = {
                    "method": "album.getinfo",
                    "api_key": api_key,
                    "artist": artist_name,
                    "album": album_title,
                    "format": "json",
                }
                async with session.get(
                    "https://ws.audioscrobbler.com/2.0/", params=params, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()

                album_info = data.get("album", {})
                # Last.fm doesn't always have year directly, but wiki may have it
                wiki = album_info.get("wiki", {})
                published = wiki.get("published", "")
                # Try to extract year from tags or release date
                year = None

                # Check tags for year-like values
                tags = album_info.get("tags", {}).get("tag", [])
                for tag in tags:
                    name = tag.get("name", "")
                    if name.isdigit() and 1900 < int(name) < 2030:
                        year = int(name)
                        break

                # Fallback: parse published date
                if not year and published:
                    parts = published.strip().split()
                    for p in parts:
                        if p.isdigit() and 1900 < int(p) < 2030:
                            year = int(p)
                            break

                if year:
                    await deps.db.execute(
                        "UPDATE albums SET year = ? WHERE id = ?", (year, album_id))
                    fixed += 1
                    results.append({"album": album_title, "artist": artist_name, "year": year})

            except Exception:
                continue

            await asyncio.sleep(0.25)

    await deps.db.commit()

    return {
        "ok": True,
        "total": len(rows),
        "fixed": fixed,
        "not_found": len(rows) - fixed,
        "details": results,
    }


# ---------------------------------------------------------------------------
# Fix missing years from Discogs
# ---------------------------------------------------------------------------

@router.post("/fix-years-discogs")
async def fix_years_from_discogs():
    """Fill missing album years by querying Discogs database API."""
    import asyncio
    import aiohttp
    import re
    from tune_server.config import settings

    token = settings.discogs_token
    if not token:
        raise HTTPException(status_code=400, detail="Discogs token not configured")

    rows = await deps.db.fetchall(
        """SELECT al.id, al.title, ar.name as artist_name
           FROM albums al
           LEFT JOIN artists ar ON al.artist_id = ar.id
           WHERE (al.year IS NULL OR al.year = 0)
           ORDER BY al.title""",
    )

    if not rows:
        return {"ok": True, "total": 0, "fixed": 0, "missing": []}

    def _clean(s: str) -> str:
        s = re.sub(r"\s*\(\d+k?hz[^)]*\)", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*\(.*?(deluxe|remaster|bonus|edition|disc).*?\)", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*-\s*Disc\s*[A-Z0-9]+$", "", s, flags=re.IGNORECASE)
        return s.strip()

    fixed = 0
    results = []
    missing = []
    headers = {
        "User-Agent": "TuneServer/0.5.7 +https://mozaiklabs.fr",
        "Authorization": f"Discogs token={token}",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        for row in rows:
            album_title = row["title"]
            artist_name = row["artist_name"] or ""
            album_id = row["id"]

            if not album_title or album_title in ("Unknown Album", ""):
                missing.append({"id": album_id, "album": album_title, "artist": artist_name})
                continue

            clean_title = _clean(album_title)
            clean_artist = _clean(artist_name)

            params = {
                "release_title": clean_title,
                "type": "release",
                "per_page": "5",
            }
            if clean_artist and clean_artist not in ("?", "Unknown Artist", "Various Interprets"):
                params["artist"] = clean_artist

            try:
                async with session.get(
                    "https://api.discogs.com/database/search",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(5)
                        async with session.get(
                            "https://api.discogs.com/database/search",
                            params=params,
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp2:
                            if resp2.status != 200:
                                missing.append({"id": album_id, "album": album_title, "artist": artist_name})
                                continue
                            data = await resp2.json()
                    elif resp.status != 200:
                        missing.append({"id": album_id, "album": album_title, "artist": artist_name})
                        continue
                    else:
                        data = await resp.json()

                hits = data.get("results", [])
                year = None
                for hit in hits:
                    y = hit.get("year")
                    if y and isinstance(y, (int, str)):
                        y = int(str(y)[:4]) if str(y)[:4].isdigit() else None
                        if y and 1900 < y < 2030:
                            year = y
                            break

                if year:
                    await deps.db.execute(
                        "UPDATE albums SET year = ? WHERE id = ?", (year, album_id))
                    fixed += 1
                    results.append({"album": album_title, "artist": artist_name, "year": year})
                else:
                    missing.append({"id": album_id, "album": album_title, "artist": artist_name})

            except Exception:
                missing.append({"id": album_id, "album": album_title, "artist": artist_name})
                continue

            # Discogs rate limit: ~60 req/min
            await asyncio.sleep(1.1)

    await deps.db.commit()

    return {
        "ok": True,
        "total": len(rows),
        "fixed": fixed,
        "not_found": len(missing),
        "details": results,
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# Fix missing years from MusicBrainz
# ---------------------------------------------------------------------------

@router.post("/fix-years-musicbrainz")
async def fix_years_from_musicbrainz():
    """Fill missing album years by querying MusicBrainz release API."""
    import asyncio
    import aiohttp
    import re
    import unicodedata

    rows = await deps.db.fetchall(
        """SELECT al.id, al.title, ar.name as artist_name
           FROM albums al
           LEFT JOIN artists ar ON al.artist_id = ar.id
           WHERE (al.year IS NULL OR al.year = 0)
           ORDER BY al.title""",
    )

    if not rows:
        return {"ok": True, "total": 0, "fixed": 0}

    def _clean(s: str) -> str:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        s = re.sub(r"\s*\(\d+k?hz[^)]*\)", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*\(.*?(deluxe|remaster|bonus|edition|live|disc).*?\)", "", s, flags=re.IGNORECASE)
        return s.strip()

    fixed = 0
    results = []
    headers = {"User-Agent": "TuneServer/0.5.7 (contact@mozaiklabs.fr)"}

    async with aiohttp.ClientSession(headers=headers) as session:
        for row in rows:
            album_title = row["title"]
            artist_name = row["artist_name"] or ""
            album_id = row["id"]

            if not album_title or album_title in ("Unknown Album", ""):
                continue

            clean_title = _clean(album_title)
            clean_artist = _clean(artist_name)

            query = f'release:"{clean_title}"'
            if clean_artist and clean_artist != "?":
                query += f' AND artist:"{clean_artist}"'

            try:
                params = {"query": query, "fmt": "json", "limit": "5"}
                async with session.get(
                    "https://musicbrainz.org/ws/2/release/",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()

                releases = data.get("releases", [])
                year = None
                for rel in releases:
                    date = rel.get("date", "")
                    if date and len(date) >= 4:
                        y = date[:4]
                        if y.isdigit() and 1900 < int(y) < 2030:
                            year = int(y)
                            break

                if year:
                    await deps.db.execute(
                        "UPDATE albums SET year = ? WHERE id = ?", (year, album_id))
                    fixed += 1
                    results.append({"album": album_title, "artist": artist_name, "year": year})

            except Exception:
                continue

            # MusicBrainz rate limit: 1 req/s
            await asyncio.sleep(1.1)

    await deps.db.commit()

    return {
        "ok": True,
        "total": len(rows),
        "fixed": fixed,
        "not_found": len(rows) - fixed,
        "details": results,
    }


# ---------------------------------------------------------------------------
# Fix missing genres from Last.fm + Discogs
# ---------------------------------------------------------------------------

_GENRE_MAP = {
    "rock": "Rock", "alternative rock": "Rock", "indie rock": "Rock",
    "classic rock": "Rock", "hard rock": "Rock", "progressive rock": "Progressive Rock",
    "post-rock": "Rock", "psychedelic rock": "Rock", "punk rock": "Punk",
    "pop": "Pop", "indie pop": "Pop", "synthpop": "Pop", "electropop": "Pop",
    "dream pop": "Pop", "chamber pop": "Pop", "art pop": "Pop",
    "jazz": "Jazz", "smooth jazz": "Jazz", "free jazz": "Jazz",
    "vocal jazz": "Jazz", "cool jazz": "Jazz", "bebop": "Jazz",
    "hard bop": "Jazz", "post-bop": "Jazz", "jazz fusion": "Jazz",
    "avant-garde jazz": "Jazz", "contemporary jazz": "Jazz",
    "electronic": "Electronic", "ambient": "Electronic", "downtempo": "Electronic",
    "idm": "Electronic", "trip-hop": "Electronic", "house": "Electronic",
    "techno": "Electronic", "electronica": "Electronic", "chillout": "Electronic",
    "classical": "Classical", "contemporary classical": "Classical",
    "modern classical": "Classical", "baroque": "Classical", "romantic": "Classical",
    "orchestral": "Classical", "chamber music": "Classical", "opera": "Classical",
    "blues": "Blues", "electric blues": "Blues", "delta blues": "Blues",
    "soul": "Soul", "neo-soul": "Soul", "r&b": "R&B", "rnb": "R&B",
    "funk": "Funk",
    "hip-hop": "Hip-Hop", "hip hop": "Hip-Hop", "rap": "Hip-Hop",
    "metal": "Metal", "heavy metal": "Metal", "progressive metal": "Metal",
    "folk": "Folk", "indie folk": "Folk", "folk rock": "Folk",
    "country": "Country", "alt-country": "Country",
    "reggae": "Reggae", "dub": "Reggae",
    "world": "World", "afrobeat": "World", "latin": "World", "bossa nova": "World",
    "chanson": "Chanson", "chanson francaise": "Chanson", "french": "Chanson",
    "singer-songwriter": "Singer-Songwriter",
    "soundtrack": "Soundtrack", "film score": "Soundtrack",
    "new wave": "New Wave", "post-punk": "New Wave",
    "experimental": "Experimental", "avant-garde": "Experimental",
}


def _normalize_genre(tags: list[str]) -> str | None:
    for tag in tags:
        normalized = _GENRE_MAP.get(tag.lower().strip())
        if normalized:
            return normalized
    for tag in tags:
        t = tag.strip()
        if len(t) > 2 and len(t) < 30 and not t.isdigit():
            return t.title()
    return None


@router.post("/fix-genres")
async def fix_genres():
    """Fill missing album genres using Last.fm tags + Discogs fallback."""
    import asyncio
    import aiohttp
    import re
    from tune_server.config import settings

    lastfm_key = settings.lastfm_api_key
    discogs_token = settings.discogs_token

    if not lastfm_key and not discogs_token:
        raise HTTPException(status_code=400, detail="No Last.fm or Discogs credentials configured")

    rows = await deps.db.fetchall(
        """SELECT al.id, al.title, ar.name as artist_name
           FROM albums al
           LEFT JOIN artists ar ON al.artist_id = ar.id
           WHERE al.genre IS NULL OR al.genre = ''
           ORDER BY al.title""",
    )

    if not rows:
        return {"ok": True, "total": 0, "fixed": 0}

    fixed = 0
    results = []
    headers_discogs = {
        "User-Agent": "TuneServer/0.5.7 +https://mozaiklabs.fr",
        "Authorization": f"Discogs token={discogs_token}",
    } if discogs_token else {}

    async with aiohttp.ClientSession() as session:
        for row in rows:
            album_title = row["title"]
            artist_name = row["artist_name"] or ""
            album_id = row["id"]

            if not album_title or album_title in ("Unknown Album", ""):
                continue

            genre = None

            # 1) Last.fm
            if lastfm_key:
                try:
                    params = {
                        "method": "album.getinfo",
                        "api_key": lastfm_key,
                        "artist": artist_name,
                        "album": re.sub(r"\s*\(\d+k?hz[^)]*\)", "", album_title, flags=re.IGNORECASE).strip(),
                        "format": "json",
                    }
                    async with session.get(
                        "https://ws.audioscrobbler.com/2.0/",
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            tags = [t["name"] for t in data.get("album", {}).get("tags", {}).get("tag", [])]
                            genre = _normalize_genre(tags)
                except Exception:
                    pass

            # 2) Discogs fallback
            if not genre and discogs_token:
                try:
                    clean = re.sub(r"\s*\(\d+k?hz[^)]*\)", "", album_title, flags=re.IGNORECASE).strip()
                    params = {"release_title": clean, "type": "release", "per_page": "3"}
                    if artist_name and artist_name not in ("Unknown Artist", "?"):
                        params["artist"] = artist_name
                    async with session.get(
                        "https://api.discogs.com/database/search",
                        params=params, headers=headers_discogs,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for hit in data.get("results", []):
                                styles = hit.get("style", []) + hit.get("genre", [])
                                genre = _normalize_genre(styles)
                                if genre:
                                    break
                        elif resp.status == 429:
                            await asyncio.sleep(5)
                except Exception:
                    pass

            if genre:
                await deps.db.execute(
                    "UPDATE albums SET genre = ? WHERE id = ?", (genre, album_id))
                fixed += 1
                results.append({"album": album_title, "artist": artist_name, "genre": genre})

            await asyncio.sleep(0.3)

    await deps.db.commit()

    return {
        "ok": True,
        "total": len(rows),
        "fixed": fixed,
        "not_found": len(rows) - fixed,
        "details": results[:100],
    }

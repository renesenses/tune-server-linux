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
    """List detected duplicate groups."""
    rows = await deps.db.fetchall(
        """SELECT d.id, d.audio_hash, d.resolved,
                  ta.id as track_a_id, ta.title as track_a_title, ta.file_path as track_a_path,
                  tb.id as track_b_id, tb.title as track_b_title, tb.file_path as track_b_path
           FROM duplicate_tracks d
           JOIN tracks ta ON ta.id = d.track_id_a
           JOIN tracks tb ON tb.id = d.track_id_b
           WHERE d.resolved = 0
           ORDER BY d.audio_hash""",
    )
    return [dict(r) for r in rows]


@router.post("/duplicates/resolve")
async def resolve_duplicate(duplicate_id: int, keep_track_id: int):
    """Resolve a duplicate — keep one track, optionally delete the other."""
    row = await deps.db.fetchone(
        "SELECT * FROM duplicate_tracks WHERE id = ?", (duplicate_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Duplicate not found")

    # Mark as resolved
    await deps.db.execute(
        "UPDATE duplicate_tracks SET resolved = 1 WHERE id = ?", (duplicate_id,))
    await deps.db.commit()

    # Determine which to remove
    remove_id = row["track_id_b"] if keep_track_id == row["track_id_a"] else row["track_id_a"]

    return {
        "ok": True,
        "kept": keep_track_id,
        "removable": remove_id,
        "note": "Track not deleted — use DELETE /library/tracks/{id} to remove",
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

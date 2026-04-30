"""Metadata Manager — suggestions, lookup, enrichment, fingerprint, auto-fix, doubtful, fix-years, fix-genres."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps
from tune_server.metadata_manager.matcher import lookup_track, lookup_album
from tune_server.metadata_manager.enricher import enrich_track, enrich_album

router = APIRouter(tags=["metadata"])

ALLOWED_METADATA_COLUMNS = {'title', 'artist_name', 'album_title', 'genre', 'year', 'track_number', 'disc_number'}


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
    """Resolve a duplicate -- keep one track, move the other to /data/duplicates/."""
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
# Doubtful metadata -- albums with inferred/uncertain data
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
             -- Artist is ALL CAPS and > 4 chars (inferred from folder name)
             (al.artist_name = UPPER(al.artist_name) AND LENGTH(al.artist_name) > 4
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
             OR (al.title = UPPER(al.title) AND LENGTH(al.title) > 4)
             -- Artist name contains year prefix (folder-derived like "1970-The Complete...")
             OR (al.artist_name LIKE '____-%' OR al.artist_name LIKE '____ %')
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
# Fix missing years from Tidal
# ---------------------------------------------------------------------------

@router.post("/fix-years-from-path")
async def fix_years_from_path():
    """Extract album year from a track's file path.

    Many libraries name folders like 'Artist - 1972 - Album', 'Album (1972)',
    '[1972] Album' or '1972 — Album'. For each album without a year, look
    at any track's file path and pull the first 4-digit year (1900-2030).
    Free, instant, no API call.
    """
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

    # Group by album (one path per album).
    seen: set[int] = set()
    albums: list[dict] = []
    for r in rows:
        aid = r["id"]
        if aid in seen:
            continue
        seen.add(aid)
        albums.append({"id": aid, "title": r["title"], "file_path": r["file_path"]})

    # Regex: match a 4-digit year that is NOT directly attached to other digits
    # (avoid catching "12345" or sample rates like "44100"). Word-boundary on
    # both sides. Range 1900-2030.
    year_re = re.compile(r"(?<!\d)(19[0-9]{2}|20[0-3][0-9])(?!\d)")

    fixed = 0
    results: list[dict] = []
    for a in albums:
        # Strip the file name itself (drop the extension and digits in track
        # numbers like "01 Track.flac"); keep just the directory path.
        from pathlib import Path
        parent = str(Path(a["file_path"]).parent)
        m = year_re.search(parent)
        if not m:
            continue
        year = int(m.group(1))
        if not (1900 <= year <= 2030):
            continue
        await deps.db.execute(
            "UPDATE albums SET year = ? WHERE id = ?", (year, a["id"]),
        )
        fixed += 1
        if len(results) < 200:
            results.append({"album": a["title"], "year": year, "path_hint": parent[-80:]})

    await deps.db.commit()

    return {
        "ok": True,
        "total": len(albums),
        "fixed": fixed,
        "not_found": len(albums) - fixed,
        "details": results,
    }


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


def _normalize_genre(tags: list[str], allowed: dict[str, str] | None = None) -> str | None:
    """Pick a genre from external service tags.

    When ``allowed`` is provided (lowercase → original-case map of the user's
    existing library genres), only return a value that is already in that
    vocabulary. We never invent new genres in that mode — better to leave the
    album empty than pollute the library with arbitrary Last.fm/Discogs tags.
    """
    if allowed is not None:
        # 1) Direct hit in the user's existing genres (case-insensitive).
        for tag in tags:
            t = tag.strip()
            if not t:
                continue
            hit = allowed.get(t.lower())
            if hit:
                return hit
        # 2) Synonym → canonical bucket via _GENRE_MAP, but only if that
        #    bucket already exists in the user's library.
        for tag in tags:
            bucket = _GENRE_MAP.get(tag.lower().strip())
            if bucket:
                hit = allowed.get(bucket.lower())
                if hit:
                    return hit
        return None

    # Legacy / unconstrained path — keep for callers that haven't migrated.
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

    # Opt-in: when settings.metadata_fix_genres_respect_vocabulary is True,
    # only assign genres that already exist in the user's library. This
    # avoids polluting a curated genre list with arbitrary Last.fm/Discogs
    # tags. Default False so a fresh install with no genres yet still gets
    # populated by external service tags.
    allowed_genres: dict[str, str] | None = None
    if settings.metadata_fix_genres_respect_vocabulary:
        genre_rows = await deps.db.fetchall(
            "SELECT DISTINCT genre FROM albums WHERE genre IS NOT NULL AND genre <> ''"
        )
        allowed_genres = {
            (r["genre"]).lower(): r["genre"] for r in genre_rows if r["genre"]
        }

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
                            genre = _normalize_genre(tags, allowed=allowed_genres)
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
                                genre = _normalize_genre(styles, allowed=allowed_genres)
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


_ARTIST_NORMALIZE_RE = re.compile(
    r"\s*("
    # Trailing suffixes
    r"(?:and\s+his|and\s+her|with\s+his|with\s+her)\s+.+|"
    r"&\s+(?:the|his|her).+|"
    r"(?:feat\.?|featuring)\s+.+|"
    r"(?:Quartet|Quintet|Trio|Sextet|Septet|Octet|Nonet|"
    r"Orchestra|Big\s*Band|Band|Ensemble|Group|Project|Combo|"
    r"All[\s-]?Stars?|All[\s-]?Star\s+\w+|Collective|Players)"
    r")\s*$",
    re.IGNORECASE,
)


# Ordered keyword → family classifier for user-curated genres. First match
# wins (so "Pop-Rock" → rock, not pop). The aim is family-level coherence
# voting: an artist whose albums are 90% jazz-something (Jazz / Contemporary
# Jazz / Jazz US / Jazz Europe) should still propagate, even though no
# specific bucket reaches the 70% threshold by itself.
_GENRE_FAMILY_RULES: list[tuple[str, str]] = [
    ("soul", "soul-funk"),
    ("funk", "soul-funk"),
    ("r&b", "soul-funk"),
    ("rnb", "soul-funk"),
    ("jazz", "jazz"),
    ("classical", "classical"),
    ("baroque", "classical"),
    ("opera", "classical"),
    ("orchestral", "classical"),
    ("blues", "blues"),
    ("chanson", "chanson"),
    ("variét", "chanson"),
    ("bossa", "world"),
    ("afro", "world"),
    ("latin", "world"),
    ("reggae", "world"),
    ("tango", "world"),
    ("world", "world"),
    ("electro", "electro"),
    ("electronic", "electro"),
    ("techno", "electro"),
    ("ambient", "electro"),
    ("idm", "electro"),
    ("house", "electro"),
    ("folk", "folk"),
    ("country", "country"),
    ("soundtrack", "soundtrack"),
    ("film", "soundtrack"),
    ("rap", "hip-hop"),
    ("hip-hop", "hip-hop"),
    ("hip hop", "hip-hop"),
    ("metal", "metal"),
    ("punk", "punk"),
    ("rock", "rock"),
    ("pop", "pop"),
]


def _genre_family(genre: str) -> str:
    g = (genre or "").lower()
    for kw, fam in _GENRE_FAMILY_RULES:
        if kw in g:
            return fam
    return "other"


def _normalize_artist_for_grouping(name: str) -> str:
    """Strip ensemble suffixes / prefixes so 'Charlie Parker', 'Charlie
    Parker Quartet', 'The Charlie Parker Quintet', 'Charlie Parker and
    his Orchestra' all collapse to 'charlie parker'."""
    if not name:
        return ""
    n = name.strip()
    # Strip leading "The "
    if n.lower().startswith("the "):
        n = n[4:]
    # Iteratively strip trailing ensemble suffix (handles "Charlie Parker
    # Quartet" but also nested "Charlie Parker All Stars Quartet").
    for _ in range(3):
        new = _ARTIST_NORMALIZE_RE.sub("", n).strip()
        if new == n:
            break
        n = new
    return n.lower().strip()


@router.post("/fix-genres-by-artist-fuzzy")
async def fix_genres_by_artist_fuzzy(min_coherence: float = 0.7):
    """Same as fix-genres-by-artist but groups artist-name variants together.

    Useful for libraries with many ensemble variants ('Charlie Parker',
    'Charlie Parker Quartet', 'The Charlie Parker Quintet', 'Charlie Parker
    and his Orchestra' — all considered the same musician for genre
    propagation). The rest of the logic (coherence threshold, skip
    Various/Unknown, no external API) is identical.
    """
    rows = await deps.db.fetchall(
        """SELECT al.id, al.title, al.genre, al.artist_id, ar.name as artist_name
           FROM albums al
           LEFT JOIN artists ar ON al.artist_id = ar.id
           WHERE al.artist_id IS NOT NULL""",
    )

    SKIP_NAMES = {
        "various artists", "various", "va", "v.a.",
        "unknown artist", "unknown", "?",
        "compilation", "compilations",
    }

    from collections import Counter, defaultdict
    by_group_genres: dict[str, Counter] = defaultdict(Counter)
    by_group_missing: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    group_display: dict[str, str] = {}

    for row in rows:
        name = (row["artist_name"] or "").strip()
        if not name or name.lower() in SKIP_NAMES:
            continue
        key = _normalize_artist_for_grouping(name)
        if not key:
            continue
        # Display name: shortest variant as the canonical label.
        if key not in group_display or len(name) < len(group_display[key]):
            group_display[key] = name
        g = (row["genre"] or "").strip()
        if g:
            by_group_genres[key][g] += 1
        else:
            by_group_missing[key].append((row["id"], row["title"] or "", name))

    fixed = 0
    skipped_low_coherence = 0
    skipped_no_known_genre = 0
    details: list[dict] = []

    for key, missing in by_group_missing.items():
        counter = by_group_genres.get(key)
        if not counter:
            skipped_no_known_genre += len(missing)
            continue
        total_known = sum(counter.values())
        top_genre, top_count = counter.most_common(1)[0]
        coherence = top_count / total_known if total_known else 0.0
        if coherence < min_coherence:
            skipped_low_coherence += len(missing)
            continue
        for album_id, title, original_artist in missing:
            await deps.db.execute(
                "UPDATE albums SET genre = ? WHERE id = ?", (top_genre, album_id),
            )
            fixed += 1
            if len(details) < 200:
                details.append({
                    "album": title,
                    "artist": original_artist,
                    "group": group_display.get(key, key),
                    "genre": top_genre,
                    "coherence": round(coherence, 2),
                    "based_on": total_known,
                })

    await deps.db.commit()

    total_candidates = sum(len(m) for m in by_group_missing.values())
    return {
        "ok": True,
        "total": total_candidates,
        "fixed": fixed,
        "skipped_low_coherence": skipped_low_coherence,
        "skipped_no_known_genre": skipped_no_known_genre,
        "min_coherence": min_coherence,
        "details": details,
    }


@router.post("/fix-genres-by-family")
async def fix_genres_by_family(min_coherence: float = 0.7):
    """Family-aware genre propagation across fuzzy artist groups.

    Same grouping as fuzzy (collapses ensemble suffixes). Then per artist
    group, classifies every known genre into a family (jazz / soul-funk /
    rock / classical / electro / world / chanson / blues / folk / country /
    soundtrack / hip-hop / metal / punk / pop / other).

    If the dominant family reaches ``min_coherence``, the most common
    specific genre within that family is propagated to the orphan albums.

    Designed for libraries with curated subgenres (Jazz, Contemporary Jazz,
    Jazz US, Jazz Europe…) where strict per-bucket voting fails because
    the count is split across siblings.
    """
    rows = await deps.db.fetchall(
        """SELECT al.id, al.title, al.genre, al.artist_id, ar.name as artist_name
           FROM albums al
           LEFT JOIN artists ar ON al.artist_id = ar.id
           WHERE al.artist_id IS NOT NULL""",
    )

    SKIP_NAMES = {
        "various artists", "various", "va", "v.a.",
        "unknown artist", "unknown", "?",
        "compilation", "compilations",
    }

    from collections import Counter, defaultdict
    # group_key -> family_name -> count_of_known_albums
    family_counts: dict[str, Counter] = defaultdict(Counter)
    # group_key -> family_name -> Counter[specific_genre]
    family_specific: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    by_group_missing: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    group_display: dict[str, str] = {}

    for row in rows:
        name = (row["artist_name"] or "").strip()
        if not name or name.lower() in SKIP_NAMES:
            continue
        key = _normalize_artist_for_grouping(name)
        if not key:
            continue
        if key not in group_display or len(name) < len(group_display[key]):
            group_display[key] = name
        g = (row["genre"] or "").strip()
        if g:
            fam = _genre_family(g)
            family_counts[key][fam] += 1
            family_specific[key][fam][g] += 1
        else:
            by_group_missing[key].append((row["id"], row["title"] or "", name))

    fixed = 0
    skipped_low_coherence = 0
    skipped_no_known_genre = 0
    skipped_only_other_family = 0
    details: list[dict] = []

    for key, missing in by_group_missing.items():
        counter = family_counts.get(key)
        if not counter:
            skipped_no_known_genre += len(missing)
            continue
        total_known = sum(counter.values())
        # Pick top family (excluding "other" — never a confident family).
        non_other = Counter({f: c for f, c in counter.items() if f != "other"})
        if not non_other:
            skipped_only_other_family += len(missing)
            continue
        top_family, top_count = non_other.most_common(1)[0]
        coherence = top_count / total_known if total_known else 0.0
        if coherence < min_coherence:
            skipped_low_coherence += len(missing)
            continue
        # Most common specific genre within that family.
        specific_counter = family_specific[key][top_family]
        target_genre, _ = specific_counter.most_common(1)[0]
        for album_id, title, original_artist in missing:
            await deps.db.execute(
                "UPDATE albums SET genre = ? WHERE id = ?", (target_genre, album_id),
            )
            fixed += 1
            if len(details) < 200:
                details.append({
                    "album": title,
                    "artist": original_artist,
                    "group": group_display.get(key, key),
                    "family": top_family,
                    "genre": target_genre,
                    "family_coherence": round(coherence, 2),
                    "based_on": total_known,
                })

    await deps.db.commit()

    total_candidates = sum(len(m) for m in by_group_missing.values())
    return {
        "ok": True,
        "total": total_candidates,
        "fixed": fixed,
        "skipped_low_coherence": skipped_low_coherence,
        "skipped_no_known_genre": skipped_no_known_genre,
        "skipped_only_other_family": skipped_only_other_family,
        "min_coherence": min_coherence,
        "details": details,
    }


@router.post("/fix-genres-by-artist")
async def fix_genres_by_artist(min_coherence: float = 0.7):
    """Propagate the dominant genre of an artist to their genre-less albums.

    For each artist, look at their albums that already have a genre. If at
    least ``min_coherence`` (default 70%) share the same value, assign that
    genre to the artist's albums that are still missing one. No external
    API call, deterministic, instant.

    Skips:
    - Artists whose albums are too inconsistent (below threshold)
    - The "Various Artists" / "VA" / Unknown placeholder so compilations are
      not poisoned by whatever the first genre happens to be.
    """
    rows = await deps.db.fetchall(
        """SELECT al.id, al.title, al.genre, al.artist_id, ar.name as artist_name
           FROM albums al
           LEFT JOIN artists ar ON al.artist_id = ar.id
           WHERE al.artist_id IS NOT NULL""",
    )

    SKIP_NAMES = {
        "various artists", "various", "va", "v.a.",
        "unknown artist", "unknown", "?",
        "compilation", "compilations",
    }

    # Group by artist_id: collect known genres + ids missing genre.
    from collections import Counter, defaultdict
    by_artist_genres: dict[int, Counter] = defaultdict(Counter)
    by_artist_missing: dict[int, list[tuple[int, str]]] = defaultdict(list)
    artist_names: dict[int, str] = {}

    for row in rows:
        aid = row["artist_id"]
        name = (row["artist_name"] or "").strip()
        if not name or name.lower() in SKIP_NAMES:
            continue
        artist_names[aid] = name
        g = (row["genre"] or "").strip()
        if g:
            by_artist_genres[aid][g] += 1
        else:
            by_artist_missing[aid].append((row["id"], row["title"] or ""))

    fixed = 0
    skipped_low_coherence = 0
    skipped_no_known_genre = 0
    details: list[dict] = []

    for aid, missing in by_artist_missing.items():
        counter = by_artist_genres.get(aid)
        if not counter:
            skipped_no_known_genre += len(missing)
            continue
        total_known = sum(counter.values())
        top_genre, top_count = counter.most_common(1)[0]
        coherence = top_count / total_known if total_known else 0.0
        if coherence < min_coherence:
            skipped_low_coherence += len(missing)
            continue
        for album_id, title in missing:
            await deps.db.execute(
                "UPDATE albums SET genre = ? WHERE id = ?", (top_genre, album_id),
            )
            fixed += 1
            if len(details) < 200:
                details.append({
                    "album": title,
                    "artist": artist_names.get(aid, "?"),
                    "genre": top_genre,
                    "coherence": round(coherence, 2),
                    "based_on": total_known,
                })

    await deps.db.commit()

    total_candidates = sum(len(m) for m in by_artist_missing.values())
    return {
        "ok": True,
        "total": total_candidates,
        "fixed": fixed,
        "skipped_low_coherence": skipped_low_coherence,
        "skipped_no_known_genre": skipped_no_known_genre,
        "min_coherence": min_coherence,
        "details": details,
    }

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from tune_server.api.deps import deps
from tune_server.config import settings
from tune_server.db.repository import full_text_search
from tune_server.event_bus import Event, EventType
from tune_server.library.artwork import copy_cover_to_album_folder, fetch_cover_from_musicbrainz, get_album_artwork, save_artwork
from tune_server.library.metadata_reader import write_tags
from tune_server.models import (
    Album,
    AlbumUpdateRequest,
    Artist,
    ArtistUpdateRequest,
    BrowseDirectory,
    BrowseResult,
    BrowseRootEntry,
    BrowseRootsResponse,
    CompletenessStats,
    LibraryStatsResponse,
    SearchResult,
    Track,
    TrackCredit,
    TrackUpdateRequest,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/library", tags=["library"])


@router.get("/tracks", response_model=list[Track])
async def list_tracks(limit: int = Query(100, le=50000), offset: int = Query(0, ge=0)):
    return await deps.track_repo.list(limit=limit, offset=offset)


@router.get("/tracks/{track_id}", response_model=Track)
async def get_track(track_id: int):
    track = await deps.track_repo.get(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    return track



@router.get("/tracks/{track_id}/credits", response_model=list[TrackCredit])
async def get_track_credits(track_id: int):
    track = await deps.track_repo.get(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if not deps.credit_repo:
        return []
    return await deps.credit_repo.list_by_track(track_id)


@router.get("/tracks/{track_id}/audio")
async def stream_track_audio(track_id: int):
    from tune_server.api.deps import deps
    track = await deps.track_repo.get(track_id)
    if not track or not track.file_path:
        raise HTTPException(status_code=404, detail="Track not found")
    filepath = Path(track.file_path)
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    suffix = filepath.suffix.lower()
    mt = {".flac": "audio/flac", ".mp3": "audio/mpeg", ".wav": "audio/wav",
          ".aac": "audio/aac", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
          ".opus": "audio/opus", ".aiff": "audio/aiff", ".dsf": "audio/dsf"}
    return FileResponse(filepath, media_type=mt.get(suffix, "application/octet-stream"), filename=filepath.name)

@router.get("/albums/recent", response_model=list[Album])
async def list_recent_albums(limit: int = Query(50, le=200)):
    """Albums recently added to the library, sorted by creation date descending."""
    return await deps.album_repo.list_recent(limit=limit)


@router.get("/albums")
async def list_albums(
    limit: int = Query(100, le=50000),
    offset: int = Query(0, ge=0),
    quality: str | None = Query(None, description="Filter by quality: hi-res, cd, lossy, dsd"),
    format: str | None = Query(None, description="Filter by format: flac, mp3, aac, wav, dsd"),
    sample_rate: int | None = Query(None, description="Filter by min sample rate in Hz (e.g. 96000)"),
):
    albums = await deps.album_repo.list(
        limit=limit, offset=offset, quality=quality,
        format=format, sample_rate=sample_rate,
    )
    result = [a.model_dump(exclude_none=False) for a in albums]

    # Add folder_path from first track of each album
    album_ids = [a.id for a in albums if a.id]
    if album_ids:
        placeholders = ", ".join(["?" for _ in album_ids])
        paths = await deps.db.fetchall(
            f"""SELECT album_id, MIN(file_path) as file_path
                FROM tracks
                WHERE album_id IN ({placeholders}) AND file_path IS NOT NULL
                GROUP BY album_id""",
            tuple(album_ids),
        )
        path_map = {}
        for r in paths:
            fp = r["file_path"]
            if fp:
                # Remove filename to get folder
                idx = fp.rfind("/")
                path_map[r["album_id"]] = fp[:idx] if idx > 0 else fp
        for item in result:
            item["folder_path"] = path_map.get(item.get("id"))

    return result


@router.get("/albums/filters")
async def get_album_filters():
    """Return available format and sample rate values for filtering."""
    rows = await deps.db.fetchall("""
        SELECT DISTINCT format, sample_rate FROM tracks
        WHERE format IS NOT NULL AND sample_rate IS NOT NULL
        ORDER BY format, sample_rate
    """)
    formats = sorted({r["format"] for r in rows if r["format"]})
    sample_rates = sorted({r["sample_rate"] for r in rows if r["sample_rate"]})
    return {"formats": formats, "sample_rates": sample_rates}


@router.get("/albums/{album_id}", response_model=Album)
async def get_album(album_id: int):
    album = await deps.album_repo.get(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    return album


@router.get("/albums/{album_id}/tracks", response_model=list[Track])
async def get_album_tracks(album_id: int):
    return await deps.track_repo.list_by_album(album_id)


@router.get("/artists", response_model=list[Artist])
async def list_artists(limit: int = Query(100, le=5000), offset: int = Query(0, ge=0)):
    return await deps.artist_repo.list(limit=limit, offset=offset)


@router.post("/artists", response_model=Artist, status_code=201)
async def create_artist(req: ArtistUpdateRequest):
    if not req.name:
        raise HTTPException(status_code=400, detail="Name is required")
    artist = Artist(name=req.name, sort_name=req.sort_name, bio=req.bio)
    artist_id = await deps.artist_repo.create(artist)
    return await deps.artist_repo.get(artist_id)


@router.get("/artists/{artist_id}", response_model=Artist)
async def get_artist(artist_id: int):
    artist = await deps.artist_repo.get(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    return artist


@router.get("/artists/{artist_id}/albums", response_model=list[Album])
async def get_artist_albums(artist_id: int):
    return await deps.album_repo.list_by_artist(artist_id)


@router.get("/artists/{artist_id}/tracks", response_model=list[Track])
async def get_artist_tracks(artist_id: int):
    return await deps.track_repo.list_by_artist(artist_id)


@router.get("/artists/{artist_id}/credits", response_model=list[TrackCredit])
async def get_artist_credits(artist_id: int):
    artist = await deps.artist_repo.get(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    if not deps.credit_repo:
        return []
    return await deps.credit_repo.list_by_artist(artist_id)


@router.get("/search", response_model=SearchResult)
async def search(q: str = Query(..., min_length=1), limit: int = Query(50, le=200)):
    return await full_text_search(deps.db, q, limit=limit)


@router.get("/stats", response_model=LibraryStatsResponse)
async def library_stats():
    return LibraryStatsResponse(
        tracks=await deps.track_repo.count(),
        albums=await deps.album_repo.count(),
        artists=await deps.artist_repo.count(),
    )


@router.put("/tracks/{track_id}", response_model=Track)
async def update_track(track_id: int, req: TrackUpdateRequest):
    track = await deps.track_repo.get(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    updates = req.model_dump(exclude_none=True)

    # Write tags to file if relevant fields changed
    if track.file_path and track.source == "local":
        tag_updates = {}
        if req.title is not None:
            tag_updates["title"] = req.title
        if req.artist_id is not None:
            artist = await deps.artist_repo.get(req.artist_id)
            if artist:
                tag_updates["artist"] = artist.name
        if req.genre is not None:
            tag_updates["genre"] = req.genre
        if req.year is not None:
            tag_updates["year"] = req.year
        if req.track_number is not None:
            tag_updates["track_number"] = req.track_number
        if req.disc_number is not None:
            tag_updates["disc_number"] = req.disc_number
        if req.album_id is not None:
            album = await deps.album_repo.get(req.album_id)
            if album:
                tag_updates["album"] = album.title
        if tag_updates and not settings.metadata_readonly:
            await asyncio.to_thread(write_tags, track.file_path, **tag_updates)

    # Genre and year belong to the album, not the track
    album_updates = {}
    if "genre" in updates:
        album_updates["genre"] = updates.pop("genre")
    if "year" in updates:
        year_val = updates.pop("year")
        try:
            album_updates["year"] = int(year_val) if year_val else None
        except (ValueError, TypeError):
            album_updates["year"] = None

    for field, value in updates.items():
        setattr(track, field, value)
    await deps.track_repo.update(track)

    # Update album genre/year if provided
    if album_updates and track.album_id:
        album = await deps.album_repo.get(track.album_id)
        if album:
            if "genre" in album_updates:
                album.genre = album_updates["genre"]
            if "year" in album_updates:
                album.year = album_updates["year"]
            await deps.album_repo.update(album)

    return await deps.track_repo.get(track_id)


@router.delete("/tracks/{track_id}", status_code=204)
async def delete_track(track_id: int):
    track = await deps.track_repo.get(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    await deps.track_repo.delete(track_id)


@router.put("/albums/{album_id}", response_model=Album)
async def update_album(album_id: int, req: AlbumUpdateRequest):
    album = await deps.album_repo.get(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    updates = req.model_dump(exclude_none=True)

    # Write album tag to all tracks in the album (unless readonly)
    if req.title is not None and not settings.metadata_readonly:
        tracks = await deps.track_repo.list_by_album(album_id)
        for t in tracks:
            if t.file_path and t.source == "local":
                await asyncio.to_thread(write_tags, t.file_path, album=req.title)

    for field, value in updates.items():
        setattr(album, field, value)
    await deps.album_repo.update(album)
    return await deps.album_repo.get(album_id)


@router.post("/albums/merge-duplicates")
async def merge_duplicate_albums():
    merged = await deps.album_repo.merge_duplicates()
    return {"merged": merged}


@router.delete("/albums/{album_id}", status_code=204)
async def delete_album(album_id: int):
    album = await deps.album_repo.get(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    await deps.album_repo.delete(album_id)


@router.put("/artists/{artist_id}", response_model=Artist)
async def update_artist(artist_id: int, req: ArtistUpdateRequest):
    artist = await deps.artist_repo.get(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    updates = req.model_dump(exclude_none=True)
    for field, value in updates.items():
        setattr(artist, field, value)
    await deps.artist_repo.update(artist)
    return await deps.artist_repo.get(artist_id)


@router.delete("/artists/{artist_id}", status_code=204)
async def delete_artist(artist_id: int):
    artist = await deps.artist_repo.get(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    await deps.artist_repo.delete(artist_id)


@router.get("/stats/completeness", response_model=CompletenessStats)
async def completeness_stats():
    # Count doubtful albums
    doubtful_row = await deps.db.fetchone(
        """SELECT count(*) as c FROM albums al
           LEFT JOIN artists ar ON al.artist_id = ar.id
           WHERE (al.artist_name = UPPER(al.artist_name) AND LENGTH(al.artist_name) > 4
                  AND al.artist_name NOT SIMILAR TO '[A-Z]{2,4}')
             OR LOWER(al.artist_name) IN ('inconnu', 'various artists', 'none')
             OR LOWER(al.genre) IN ('other', 'divers')
             OR (al.year IS NOT NULL AND al.year > 0 AND (al.year < 1920 OR al.year > 2026))
             OR (al.title = UPPER(al.title) AND LENGTH(al.title) > 4
                 AND al.title NOT SIMILAR TO '[A-Z]{2,4}')
             OR al.artist_name ~ '^\d{4}[-\s]'""",
    )
    return CompletenessStats(
        total_albums=await deps.album_repo.count(),
        albums_without_cover=await deps.album_repo.count_without_cover(),
        albums_without_genre=await deps.album_repo.count_without_genre(),
        albums_without_year=await deps.album_repo.count_without_year(),
        total_artists=await deps.artist_repo.count(),
        artists_without_image=await deps.artist_repo.count_without_image(),
        total_tracks=await deps.track_repo.count(),
        tracks_without_artist=await deps.track_repo.count_without_artist(),
        doubtful_count=doubtful_row["c"] if doubtful_row else 0,
    )


@router.get("/artwork/{filename:path}")
async def get_artwork(filename: str):
    """Serve artwork images from the cache directory."""
    # Try artwork_cache subdirectory first, then direct path
    cache_dir = Path(settings.artwork_cache_dir)
    file_path = cache_dir / filename
    if not file_path.exists():
        # Maybe filename includes "artwork_cache/" prefix
        file_path = Path(settings.artwork_cache_dir).parent / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Artwork not found")
    # Security: ensure the path is within the cache directory
    try:
        file_path.resolve().relative_to(cache_dir.resolve().parent)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    mime = "image/jpeg" if file_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return FileResponse(str(file_path), media_type=mime)


@router.post("/albums/{album_id}/artwork", response_model=Album)
async def upload_album_artwork(album_id: int, file: UploadFile):
    album = await deps.album_repo.get(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    image_data = await file.read()
    if not image_data:
        raise HTTPException(status_code=400, detail="Empty file")

    cover_path = await asyncio.to_thread(
        save_artwork, f"upload:album:{album_id}", image_data, True,
    )
    if not cover_path:
        raise HTTPException(status_code=500, detail="Failed to save artwork")

    album.cover_path = cover_path
    await deps.album_repo.update(album)

    # Copy cover.jpg to album folder
    tracks = await deps.track_repo.list_by_album(album_id)
    if tracks and tracks[0].file_path:
        await asyncio.to_thread(copy_cover_to_album_folder, cover_path, tracks[0].file_path)

    return await deps.album_repo.get(album_id)


@router.post("/albums/{album_id}/artwork/rescan")
async def rescan_album_artwork(album_id: int):
    album = await deps.album_repo.get(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    cover_path = None

    # Try local extraction from album tracks
    tracks = await deps.track_repo.list_by_album(album_id)
    for track in tracks:
        cover_path = await asyncio.to_thread(get_album_artwork, track.file_path)
        if cover_path:
            break

    # Fallback: MusicBrainz
    if not cover_path and album.artist_name and album.title:
        cover_path = await asyncio.to_thread(
            fetch_cover_from_musicbrainz,
            album.artist_name,
            album.title,
            settings.artwork_cache_dir,
        )

    if cover_path:
        album.cover_path = cover_path
        await deps.album_repo.update(album)

        # Copy cover.jpg to album folder
        if tracks and tracks[0].file_path:
            await asyncio.to_thread(copy_cover_to_album_folder, cover_path, tracks[0].file_path)

        return {"status": "found", "cover_path": cover_path}

    return {"status": "not_found", "cover_path": None}


_artwork_rescan_running = False


async def _rescan_artwork_task() -> None:
    global _artwork_rescan_running
    _artwork_rescan_running = True
    try:
        # Albums without cover + albums whose cover file is missing
        albums = await deps.album_repo.list_without_cover()
        all_with_cover = await deps.album_repo.list(limit=100000, offset=0)
        missing_ids = {a.id for a in albums}
        for album in all_with_cover:
            if album.id not in missing_ids and album.cover_path:
                if not Path(album.cover_path).exists():
                    albums.append(album)
        total = len(albums)
        found = 0
        logger.info("artwork_rescan_started", total=total)

        for i, album in enumerate(albums):
            # Try local extraction first (from any track of the album)
            tracks = await deps.track_repo.list_by_album(album.id)
            cover_path = None
            for track in tracks:
                cover_path = await asyncio.to_thread(get_album_artwork, track.file_path)
                if cover_path:
                    break

            # Fallback: MusicBrainz
            if not cover_path and album.artist_name and album.title:
                cover_path = await asyncio.to_thread(
                    fetch_cover_from_musicbrainz,
                    album.artist_name,
                    album.title,
                    settings.artwork_cache_dir,
                )

            if cover_path:
                album.cover_path = cover_path
                await deps.album_repo.update(album)
                found += 1

                # Copy cover.jpg to album folder
                if tracks and tracks[0].file_path:
                    await asyncio.to_thread(copy_cover_to_album_folder, cover_path, tracks[0].file_path)

            # Emit progress every album
            await deps.event_bus.emit(Event(
                type=EventType.LIBRARY_ARTWORK_PROGRESS,
                data={"current": i + 1, "total": total, "found": found},
                source="artwork_rescan",
            ))

        logger.info("artwork_rescan_completed", total=total, found=found)
        await deps.event_bus.emit(Event(
            type=EventType.LIBRARY_ARTWORK_COMPLETED,
            data={"total": total, "found": found},
            source="artwork_rescan",
        ))
    except Exception:
        logger.exception("artwork_rescan_error")
    finally:
        _artwork_rescan_running = False


@router.post("/artwork/rescan")
async def rescan_artwork():
    if _artwork_rescan_running:
        return {"status": "already_running"}
    asyncio.create_task(_rescan_artwork_task())
    return {"status": "started"}


# --- Browse by directory ---


def _is_path_under_roots(path: str) -> str | None:
    """Validate that path is under a configured music_dir. Returns the matching root or None."""
    try:
        resolved = Path(path).resolve()
    except (ValueError, OSError):
        return None
    for music_dir in settings.music_dirs:
        root = Path(music_dir).resolve()
        if resolved == root or str(resolved).startswith(str(root) + os.sep):
            return str(root)
    return None


@router.get("/browse", response_model=BrowseRootsResponse)
async def browse_roots():
    """List configured music directories with track counts."""
    # Build mount_path → friendly name from network mounts + discovered devices
    mount_display: dict[str, str] = {}
    if deps.mount_manager:
        mounts = await deps.mount_manager.list_mounts()
        # host → device name from discovered DLNA renderers
        device_names: dict[str, str] = {}
        if deps.discovery_manager:
            for dev in deps.discovery_manager.list_devices():
                if dev.host:
                    device_names[dev.host] = dev.name
        for m in mounts:
            if m.status == "mounted":
                name = device_names.get(m.host, m.host)
                mount_display[m.mount_path] = name

    roots = []
    for music_dir in settings.music_dirs:
        resolved = str(Path(music_dir).resolve()).replace("\\", "/")
        count = await deps.track_repo.count_by_root(resolved)
        name = mount_display.get(resolved, Path(resolved).name)
        roots.append(BrowseRootEntry(
            name=name,
            path=resolved,
            track_count=count,
        ))
    return BrowseRootsResponse(roots=roots)


@router.get("/browse/dir", response_model=BrowseResult)
async def browse_directory(path: str = Query(..., description="Absolute path to browse")):
    """Browse a directory: returns subdirectories and tracks."""
    music_root = _is_path_under_roots(path)
    if music_root is None:
        raise HTTPException(status_code=403, detail="Path is not under a configured music directory")

    resolved = str(Path(path).resolve())

    subdirs = await deps.track_repo.list_subdirectories(resolved)
    tracks = await deps.track_repo.list_by_directory(resolved)

    directories = [
        BrowseDirectory(name=d["name"], path=d["path"], track_count=d["track_count"])
        for d in subdirs
    ]

    # Compute parent (None if we're at the music root)
    parent = None
    if resolved != music_root:
        parent = str(Path(resolved).parent)

    return BrowseResult(
        path=resolved,
        parent=parent,
        music_root=music_root,
        directories=directories,
        tracks=tracks,
    )

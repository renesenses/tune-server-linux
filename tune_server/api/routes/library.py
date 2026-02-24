from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from tune_server.api.deps import deps
from tune_server.config import settings
from tune_server.db.repository import full_text_search
from tune_server.models import (
    Album,
    AlbumUpdateRequest,
    Artist,
    ArtistUpdateRequest,
    LibraryStatsResponse,
    SearchResult,
    Track,
    TrackUpdateRequest,
)

router = APIRouter(prefix="/library", tags=["library"])


@router.get("/tracks", response_model=list[Track])
async def list_tracks(limit: int = Query(100, le=500), offset: int = Query(0, ge=0)):
    return await deps.track_repo.list(limit=limit, offset=offset)


@router.get("/tracks/{track_id}", response_model=Track)
async def get_track(track_id: int):
    track = await deps.track_repo.get(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    return track


@router.get("/albums", response_model=list[Album])
async def list_albums(limit: int = Query(100, le=500), offset: int = Query(0, ge=0)):
    return await deps.album_repo.list(limit=limit, offset=offset)


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
async def list_artists(limit: int = Query(100, le=500), offset: int = Query(0, ge=0)):
    return await deps.artist_repo.list(limit=limit, offset=offset)


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
    for field, value in updates.items():
        setattr(track, field, value)
    await deps.track_repo.update(track)
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
    for field, value in updates.items():
        setattr(album, field, value)
    await deps.album_repo.update(album)
    return await deps.album_repo.get(album_id)


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


@router.get("/artwork/{filename}")
async def get_artwork(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    filepath = Path(settings.artwork_cache_dir) / filename
    if not filepath.is_file():
        raise HTTPException(status_code=404, detail="Artwork not found")
    suffix = filepath.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    media_type = media_types.get(suffix, "application/octet-stream")
    return FileResponse(filepath, media_type=media_type)

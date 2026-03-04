from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from tune_server.api.deps import deps
from tune_server.event_bus import Event, EventType
from tune_server.models import (
    Playlist,
    PlaylistAddTracksRequest,
    PlaylistCreateRequest,
    PlaylistReorderRequest,
    PlaylistUpdateRequest,
    Track,
)

router = APIRouter(prefix="/playlists", tags=["playlists"])


@router.post("", response_model=Playlist, status_code=201)
async def create_playlist(req: PlaylistCreateRequest):
    playlist_id = await deps.playlist_repo.create(req.name, req.description)
    playlist = await deps.playlist_repo.get(playlist_id)
    deps.event_bus.emit_nowait(Event(
        type=EventType.PLAYLIST_CREATED,
        data={"playlist_id": playlist_id, "name": req.name},
        source="playlists",
    ))
    return playlist


@router.get("", response_model=list[Playlist])
async def list_playlists(limit: int = 100, offset: int = 0):
    return await deps.playlist_repo.list(limit=limit, offset=offset)


@router.get("/{playlist_id}", response_model=Playlist)
async def get_playlist(playlist_id: int):
    playlist = await deps.playlist_repo.get(playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return playlist


@router.put("/{playlist_id}", response_model=Playlist)
async def update_playlist(playlist_id: int, req: PlaylistUpdateRequest):
    existing = await deps.playlist_repo.get(playlist_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Playlist not found")
    await deps.playlist_repo.update(playlist_id, name=req.name, description=req.description)
    updated = await deps.playlist_repo.get(playlist_id)
    deps.event_bus.emit_nowait(Event(
        type=EventType.PLAYLIST_UPDATED,
        data={"playlist_id": playlist_id, "name": updated.name},
        source="playlists",
    ))
    return updated


@router.delete("/{playlist_id}", status_code=204)
async def delete_playlist(playlist_id: int):
    existing = await deps.playlist_repo.get(playlist_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Playlist not found")
    await deps.playlist_repo.delete(playlist_id)
    deps.event_bus.emit_nowait(Event(
        type=EventType.PLAYLIST_DELETED,
        data={"playlist_id": playlist_id},
        source="playlists",
    ))
    return JSONResponse(status_code=204, content=None)


@router.get("/{playlist_id}/tracks", response_model=list[Track])
async def get_playlist_tracks(playlist_id: int):
    existing = await deps.playlist_repo.get(playlist_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return await deps.playlist_repo.get_tracks(playlist_id)


@router.post("/{playlist_id}/tracks", response_model=Playlist)
async def add_playlist_tracks(playlist_id: int, req: PlaylistAddTracksRequest):
    existing = await deps.playlist_repo.get(playlist_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Playlist not found")

    all_track_ids = list(req.track_ids)

    # Upsert streaming tracks into the tracks table
    for st in req.streaming_tracks:
        track = await deps.track_repo.get_by_source(st.source, st.source_id)
        if not track:
            track_obj = Track(
                title=st.title,
                artist_name=st.artist_name,
                album_title=st.album_title,
                duration_ms=st.duration_ms,
                format=st.format,
                sample_rate=st.sample_rate,
                bit_depth=st.bit_depth,
                channels=st.channels,
                cover_path=st.cover_path,
                source=st.source,
                source_id=st.source_id,
            )
            track_id = await deps.track_repo.create(track_obj)
        else:
            track_id = track.id
        all_track_ids.append(track_id)

    if all_track_ids:
        await deps.playlist_repo.add_tracks(playlist_id, all_track_ids, req.position)
    deps.event_bus.emit_nowait(Event(
        type=EventType.PLAYLIST_TRACKS_CHANGED,
        data={"playlist_id": playlist_id},
        source="playlists",
    ))
    return await deps.playlist_repo.get(playlist_id)


@router.delete("/{playlist_id}/tracks/{track_id}", status_code=204)
async def remove_playlist_track(playlist_id: int, track_id: int):
    existing = await deps.playlist_repo.get(playlist_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Playlist not found")
    await deps.playlist_repo.remove_track(playlist_id, track_id)
    deps.event_bus.emit_nowait(Event(
        type=EventType.PLAYLIST_TRACKS_CHANGED,
        data={"playlist_id": playlist_id},
        source="playlists",
    ))
    return JSONResponse(status_code=204, content=None)


@router.put("/{playlist_id}/tracks", response_model=list[Track])
async def reorder_playlist_tracks(playlist_id: int, req: PlaylistReorderRequest):
    existing = await deps.playlist_repo.get(playlist_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Playlist not found")
    await deps.playlist_repo.reorder_tracks(playlist_id, req.track_ids)
    deps.event_bus.emit_nowait(Event(
        type=EventType.PLAYLIST_TRACKS_CHANGED,
        data={"playlist_id": playlist_id},
        source="playlists",
    ))
    return await deps.playlist_repo.get_tracks(playlist_id)

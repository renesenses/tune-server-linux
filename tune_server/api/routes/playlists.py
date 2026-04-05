from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from tune_server.api.deps import deps
from tune_server.event_bus import Event, EventType
from tune_server.models import (
    DiffTrackResult,
    Playlist,
    PlaylistAddTracksRequest,
    PlaylistCreateRequest,
    PlaylistDiffRequest,
    PlaylistDiffResponse,
    PlaylistImportRequest,
    PlaylistImportResponse,
    PlaylistReorderRequest,
    PlaylistTransferRequest,
    PlaylistTransferResponse,
    PlaylistUpdateRequest,
    StreamingTrackInfo,
    Track,
    TrackMatchRequest,
    TransferTrackResult,
    UnifiedPlaylistsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/playlists", tags=["playlists"])


@router.get("/all", response_model=UnifiedPlaylistsResponse)
async def list_all_playlists():
    """Return all playlists from local DB and all authenticated streaming services."""
    local = await deps.playlist_repo.list(limit=500)

    async def _fetch(name: str, svc):
        try:
            return name, await svc.get_user_playlists()
        except Exception:
            logger.debug("Failed to fetch playlists from %s", name, exc_info=True)
            return name, []

    tasks = [
        _fetch(name, svc)
        for name, svc in deps.streaming_services.items()
        if svc.is_authenticated
    ]
    results = await asyncio.gather(*tasks)
    services = {name: pls for name, pls in results if pls}
    return UnifiedPlaylistsResponse(local=local, services=services)


@router.post("/import", response_model=PlaylistImportResponse)
async def import_playlist(body: PlaylistImportRequest):
    """Import a streaming playlist into a new local playlist."""
    svc = deps.streaming_services.get(body.service)
    if not svc:
        raise HTTPException(status_code=503, detail=f"{body.service} not configured")
    if not svc.is_authenticated:
        raise HTTPException(status_code=503, detail=f"{body.service} not authenticated")

    # Fetch tracks from the streaming service
    tracks = await svc.get_playlist_tracks(body.playlist_id)

    # Resolve playlist name
    name = body.name
    if not name:
        playlists = await svc.get_user_playlists()
        match = next((p for p in playlists if p.source_id == body.playlist_id), None)
        name = match.name if match else f"Import from {body.service}"

    # Create local playlist
    playlist_id = await deps.playlist_repo.create(name)

    # Upsert streaming tracks and collect IDs
    all_track_ids: list[int] = []
    for t in tracks:
        if t.source and t.source_id:
            existing = await deps.track_repo.get_by_source(t.source, t.source_id)
            if existing:
                all_track_ids.append(existing.id)
            else:
                st = StreamingTrackInfo(
                    source=t.source,
                    source_id=t.source_id,
                    title=t.title,
                    artist_name=t.artist_name,
                    album_title=t.album_title,
                    duration_ms=t.duration_ms,
                    format=t.format,
                    sample_rate=t.sample_rate,
                    bit_depth=t.bit_depth,
                    channels=t.channels,
                    cover_path=t.cover_path,
                )
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
                all_track_ids.append(track_id)

    if all_track_ids:
        await deps.playlist_repo.add_tracks(playlist_id, all_track_ids)

    deps.event_bus.emit_nowait(Event(
        type=EventType.PLAYLIST_CREATED,
        data={"playlist_id": playlist_id, "name": name},
        source="playlists",
    ))
    return PlaylistImportResponse(playlist_id=playlist_id, name=name, tracks_imported=len(all_track_ids))


@router.post("/match")
async def match_track(body: TrackMatchRequest):
    """Find track equivalents across streaming services and local library."""
    results: dict[str, object] = {}
    query = f"{body.artist_name} {body.title}"

    async def _search(name: str, svc):
        try:
            search = await svc.search(query, limit=3)
            for t in search.tracks:
                if body.title.lower() in t.title.lower():
                    return name, t
        except Exception:
            logger.debug("Match search failed on %s", name, exc_info=True)
        return name, None

    tasks = []
    for name, svc in deps.streaming_services.items():
        if body.services and name not in body.services:
            continue
        if not svc.is_authenticated:
            continue
        tasks.append(_search(name, svc))

    search_results = await asyncio.gather(*tasks)
    for name, track in search_results:
        if track:
            results[name] = track

    # Also check local library
    local = await deps.track_repo.search(query, limit=3)
    if local:
        results["local"] = local[0]

    return results


def _normalize(s: str) -> str:
    """Normalize for comparison: lowercase, strip feat/remix/remaster/parens."""
    s = s.lower().strip()
    s = re.sub(r'\(.*?\)', '', s)
    s = re.sub(r'\[.*?\]', '', s)
    s = re.sub(r'\s*-\s*(feat|ft|featuring|remix|remaster|deluxe|bonus).*', '', s, flags=re.IGNORECASE)
    return s.strip()


def _fuzzy_match_track(title1: str, artist1: str, title2: str, artist2: str) -> str | None:
    """Returns 'exact', 'approximate', or None."""
    t1 = _normalize(title1)
    t2 = _normalize(title2)
    a1 = _normalize(artist1)
    a2 = _normalize(artist2)
    if t1 == t2 and a1 == a2:
        return "exact"
    if t1 == t2 or (a1 == a2 and (t1 in t2 or t2 in t1)):
        return "approximate"
    return None


async def _get_playlist_tracks_and_name(
    service: str, playlist_id: str
) -> tuple[list[Track], str]:
    """Get tracks and name for a playlist from any service."""
    if service == "local":
        pid = int(playlist_id)
        playlist = await deps.playlist_repo.get(pid)
        if not playlist:
            raise HTTPException(status_code=404, detail="Local playlist not found")
        tracks = await deps.playlist_repo.get_tracks(pid)
        return tracks, playlist.name
    else:
        svc = deps.streaming_services.get(service)
        if not svc:
            raise HTTPException(status_code=503, detail=f"{service} not configured")
        if not svc.is_authenticated:
            raise HTTPException(status_code=503, detail=f"{service} not authenticated")
        tracks = await svc.get_playlist_tracks(playlist_id)
        # Resolve name
        playlists = await svc.get_user_playlists()
        match = next((p for p in playlists if p.source_id == playlist_id), None)
        name = match.name if match else f"Playlist from {service}"
        return tracks, name


@router.post("/transfer", response_model=PlaylistTransferResponse)
async def transfer_playlist(body: PlaylistTransferRequest):
    """Transfer a playlist from one service to another with track matching."""
    # 1. Get source tracks
    source_tracks, source_name = await _get_playlist_tracks_and_name(
        body.source_service, body.source_playlist_id
    )
    target_name = body.target_name or source_name

    # 2. For each track, search on target service
    results: list[TransferTrackResult] = []
    matched_track_ids: list[int] = []

    target_svc = None
    if body.target_service != "local":
        target_svc = deps.streaming_services.get(body.target_service)
        if not target_svc:
            raise HTTPException(status_code=503, detail=f"{body.target_service} not configured")
        if not target_svc.is_authenticated:
            raise HTTPException(status_code=503, detail=f"{body.target_service} not authenticated")

    for t in source_tracks:
        artist = t.artist_name or ""
        query = f"{artist} {t.title}"
        result = TransferTrackResult(
            title=t.title,
            artist_name=t.artist_name,
            status="not_found",
            source_id=t.source_id or (str(t.id) if t.id else None),
        )

        try:
            if body.target_service == "local":
                # Search local library
                local_results = await deps.track_repo.search(query, limit=5)
                for lr in local_results:
                    quality = _fuzzy_match_track(
                        t.title, artist, lr.title, lr.artist_name or ""
                    )
                    if quality:
                        result.status = "matched" if quality == "exact" else "approximate"
                        result.target_id = str(lr.id)
                        result.target_service = "local"
                        matched_track_ids.append(lr.id)
                        break
            else:
                # Search on target streaming service
                search_result = await target_svc.search(query, limit=5)
                for sr in search_result.tracks:
                    quality = _fuzzy_match_track(
                        t.title, artist, sr.title, sr.artist_name or ""
                    )
                    if quality:
                        result.status = "matched" if quality == "exact" else "approximate"
                        result.target_id = sr.source_id
                        result.target_service = body.target_service
                        break
        except Exception:
            logger.debug("Transfer search failed for '%s'", query, exc_info=True)

        results.append(result)

    # 3. Create local playlist with matched tracks
    playlist_id = await deps.playlist_repo.create(target_name)

    if body.target_service == "local":
        # Add local track IDs directly
        if matched_track_ids:
            await deps.playlist_repo.add_tracks(playlist_id, matched_track_ids)
    else:
        # Upsert streaming tracks from target service into the playlist
        upserted_ids: list[int] = []
        for r in results:
            if r.status in ("matched", "approximate") and r.target_id:
                existing = await deps.track_repo.get_by_source(body.target_service, r.target_id)
                if existing:
                    upserted_ids.append(existing.id)
                else:
                    track_obj = Track(
                        title=r.title,
                        artist_name=r.artist_name,
                        source=body.target_service,
                        source_id=r.target_id,
                    )
                    track_id = await deps.track_repo.create(track_obj)
                    upserted_ids.append(track_id)
        if upserted_ids:
            await deps.playlist_repo.add_tracks(playlist_id, upserted_ids)

    deps.event_bus.emit_nowait(Event(
        type=EventType.PLAYLIST_CREATED,
        data={"playlist_id": playlist_id, "name": target_name},
        source="playlists",
    ))

    matched_count = sum(1 for r in results if r.status == "matched")
    approximate_count = sum(1 for r in results if r.status == "approximate")
    not_found_count = sum(1 for r in results if r.status == "not_found")

    return PlaylistTransferResponse(
        playlist_id=playlist_id,
        playlist_name=target_name,
        total_tracks=len(source_tracks),
        matched=matched_count,
        not_found=not_found_count,
        approximate=approximate_count,
        tracks=results,
    )


@router.post("/diff", response_model=PlaylistDiffResponse)
async def diff_playlists(body: PlaylistDiffRequest):
    """Compare two playlists from any services and return a diff."""
    source_tracks, source_name = await _get_playlist_tracks_and_name(
        body.source_service, body.source_playlist_id
    )
    target_tracks, target_name = await _get_playlist_tracks_and_name(
        body.target_service, body.target_playlist_id
    )

    in_both: list[DiffTrackResult] = []
    only_in_source: list[DiffTrackResult] = []
    target_matched: set[int] = set()  # indices of matched target tracks

    for st in source_tracks:
        found = False
        for idx, tt in enumerate(target_tracks):
            if idx in target_matched:
                continue
            quality = _fuzzy_match_track(
                st.title, st.artist_name or "",
                tt.title, tt.artist_name or "",
            )
            if quality:
                in_both.append(DiffTrackResult(
                    title=st.title,
                    artist_name=st.artist_name,
                    in_source=True,
                    in_target=True,
                    match_quality=quality,
                ))
                target_matched.add(idx)
                found = True
                break
        if not found:
            only_in_source.append(DiffTrackResult(
                title=st.title,
                artist_name=st.artist_name,
                in_source=True,
                in_target=False,
            ))

    only_in_target: list[DiffTrackResult] = []
    for idx, tt in enumerate(target_tracks):
        if idx not in target_matched:
            only_in_target.append(DiffTrackResult(
                title=tt.title,
                artist_name=tt.artist_name,
                in_source=False,
                in_target=True,
            ))

    return PlaylistDiffResponse(
        source_name=source_name,
        target_name=target_name,
        only_in_source=only_in_source,
        only_in_target=only_in_target,
        in_both=in_both,
    )


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

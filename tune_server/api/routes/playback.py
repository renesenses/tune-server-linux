from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps
from tune_server.models import (
    PlayRequest,
    QueueAddRequest,
    QueueJumpRequest,
    QueueLengthResponse,
    QueueMoveRequest,
    QueueStateResponse,
    RepeatMode,
    RepeatResponse,
    SeekRequest,
    ShuffleResponse,
    VolumeRequest,
    Zone,
)

router = APIRouter(prefix="/zones/{zone_id}", tags=["playback"])


def _get_zone(zone_id: int):
    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")
    return zone


async def _resolve_tracks(request: PlayRequest) -> list:
    """Resolve a PlayRequest to a list of Track objects."""
    tracks = []

    if request.track_id:
        track = await deps.track_repo.get(request.track_id)
        if track:
            tracks.append(track)

    elif request.track_ids:
        tracks = await deps.track_repo.get_multiple(request.track_ids)

    elif request.album_id:
        tracks = await deps.track_repo.list_by_album(request.album_id)

    elif request.playlist_id and deps.playlist_repo:
        tracks = await deps.playlist_repo.get_tracks(request.playlist_id)

    elif request.source and request.streaming_playlist_id:
        # Streaming playlist — resolve all tracks + URLs
        service = deps.streaming_services.get(request.source.value)
        if service and service.is_authenticated:
            playlist_tracks = await service.get_playlist_tracks(request.streaming_playlist_id)

            async def resolve(t):
                url = await service.get_stream_url(t.source_id)
                if url:
                    t.file_path = url
                return t

            resolved = await asyncio.gather(*[resolve(t) for t in playlist_tracks])
            tracks = [t for t in resolved if t.file_path]

    elif request.source and request.streaming_album_id:
        # Streaming album — resolve all tracks + URLs
        service = deps.streaming_services.get(request.source.value)
        if service and service.is_authenticated:
            album_tracks = await service.get_album_tracks(request.streaming_album_id)

            async def resolve_url(t):
                url = await service.get_stream_url(t.source_id)
                if url:
                    t.file_path = url
                return t

            resolved = await asyncio.gather(*[resolve_url(t) for t in album_tracks])
            tracks = [t for t in resolved if t.file_path]

    elif request.source and request.source_id:
        # Streaming service track — resolve track metadata AND stream URL
        service = deps.streaming_services.get(request.source.value)
        if service and service.is_authenticated:
            track = await service.get_track(request.source_id)
            if track:
                url = await service.get_stream_url(request.source_id)
                if url:
                    track.file_path = url
                    tracks.append(track)

    return tracks


@router.post("/play", response_model=Zone)
async def play(zone_id: int, request: PlayRequest = None):
    zone = _get_zone(zone_id)
    request = request or PlayRequest()

    tracks = await _resolve_tracks(request)

    has_play_target = (
        request.track_id or request.track_ids or request.album_id
        or request.playlist_id or request.source_id
        or request.streaming_album_id or request.streaming_playlist_id
    )

    if has_play_target and not tracks:
        raise HTTPException(
            status_code=422,
            detail="Could not resolve track(s) for playback",
        )

    # If zone is in a group, play on all group members
    group = deps.group_manager.get_group_for_zone(zone_id) if deps.group_manager else None
    if group and tracks:
        await group.play(tracks)
    elif tracks:
        await zone.player.play(tracks=tracks)
    else:
        # Resume current queue (no specific track requested)
        await zone.player.play()

    return zone.to_model()


@router.post("/pause", response_model=Zone)
async def pause(zone_id: int):
    zone = _get_zone(zone_id)
    group = deps.group_manager.get_group_for_zone(zone_id) if deps.group_manager else None
    if group:
        await group.pause()
    else:
        await zone.player.pause()
    return zone.to_model()


@router.post("/resume", response_model=Zone)
async def resume(zone_id: int):
    zone = _get_zone(zone_id)
    group = deps.group_manager.get_group_for_zone(zone_id) if deps.group_manager else None
    if group:
        await group.resume()
    else:
        await zone.player.resume()
    return zone.to_model()


@router.post("/stop", response_model=Zone)
async def stop(zone_id: int):
    zone = _get_zone(zone_id)
    group = deps.group_manager.get_group_for_zone(zone_id) if deps.group_manager else None
    if group:
        await group.stop()
    else:
        await zone.player.stop()
    return zone.to_model()


@router.post("/next", response_model=Zone)
async def skip_next(zone_id: int):
    zone = _get_zone(zone_id)
    group = deps.group_manager.get_group_for_zone(zone_id) if deps.group_manager else None
    if group:
        await group.skip_next()
    else:
        await zone.player.skip_next()
    return zone.to_model()


@router.post("/previous", response_model=Zone)
async def skip_previous(zone_id: int):
    zone = _get_zone(zone_id)
    group = deps.group_manager.get_group_for_zone(zone_id) if deps.group_manager else None
    if group:
        await group.skip_previous()
    else:
        await zone.player.skip_previous()
    return zone.to_model()


@router.post("/seek", response_model=Zone)
async def seek(zone_id: int, request: SeekRequest):
    zone = _get_zone(zone_id)
    await zone.player.seek(request.position_ms)
    return zone.to_model()


@router.post("/volume", response_model=Zone)
async def set_volume(zone_id: int, request: VolumeRequest):
    zone = _get_zone(zone_id)
    await zone.player.set_volume(request.volume)
    return zone.to_model()


@router.post("/shuffle", response_model=ShuffleResponse)
async def toggle_shuffle(zone_id: int, enabled: bool = True):
    zone = _get_zone(zone_id)
    zone.player.queue.shuffle = enabled
    return ShuffleResponse(shuffle=zone.player.queue.shuffle)


@router.post("/repeat", response_model=RepeatResponse)
async def set_repeat(zone_id: int, mode: RepeatMode = RepeatMode.OFF):
    zone = _get_zone(zone_id)
    zone.player.queue.repeat = mode
    return RepeatResponse(repeat=zone.player.queue.repeat)


@router.get("/queue", response_model=QueueStateResponse)
async def get_queue(zone_id: int):
    zone = _get_zone(zone_id)
    return QueueStateResponse(
        tracks=zone.player.queue.tracks,
        position=zone.player.queue.position,
        length=zone.player.queue.length,
    )


@router.post("/queue/add", response_model=QueueLengthResponse)
async def add_to_queue(zone_id: int, request: QueueAddRequest):
    zone = _get_zone(zone_id)
    tracks = []

    if request.track_id:
        track = await deps.track_repo.get(request.track_id)
        if track:
            tracks.append(track)
    elif request.track_ids:
        tracks = await deps.track_repo.get_multiple(request.track_ids)
    elif request.album_id:
        tracks = await deps.track_repo.list_by_album(request.album_id)
    elif request.source and request.source_id:
        service = deps.streaming_services.get(request.source.value)
        if service and service.is_authenticated:
            track = await service.get_track(request.source_id)
            if track:
                url = await service.get_stream_url(request.source_id)
                if url:
                    track.file_path = url
                    tracks.append(track)

    if tracks:
        zone.player.queue.add_tracks(tracks, position=request.position)

    return QueueLengthResponse(queue_length=zone.player.queue.length)


@router.delete("/queue/{index}", response_model=QueueLengthResponse)
async def remove_from_queue(zone_id: int, index: int):
    zone = _get_zone(zone_id)
    queue = zone.player.queue
    if index < 0 or index >= queue.length:
        raise HTTPException(status_code=400, detail="Invalid queue index")

    is_current = index == queue.position
    queue.remove_track(index)

    if is_current:
        if queue.current:
            await zone.player.play()
        else:
            await zone.player.stop()

    return QueueLengthResponse(queue_length=queue.length)


@router.post("/queue/move", response_model=QueueLengthResponse)
async def move_in_queue(zone_id: int, request: QueueMoveRequest):
    zone = _get_zone(zone_id)
    ok = zone.player.queue.move_track(request.from_position, request.to_position)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid queue positions")
    return QueueLengthResponse(queue_length=zone.player.queue.length)


@router.post("/queue/jump", response_model=Zone)
async def jump_in_queue(zone_id: int, request: QueueJumpRequest):
    zone = _get_zone(zone_id)
    track = zone.player.queue.jump_to(request.position)
    if not track:
        raise HTTPException(status_code=400, detail="Invalid queue position")
    await zone.player.play()
    return zone.to_model()


@router.post("/queue/clear", response_model=QueueLengthResponse)
async def clear_queue(zone_id: int):
    zone = _get_zone(zone_id)
    zone.player.queue.clear()
    await zone.player.stop()
    return QueueLengthResponse(queue_length=0)


@router.get("/status", response_model=Zone)
async def get_status(zone_id: int):
    zone = _get_zone(zone_id)
    return zone.to_model()

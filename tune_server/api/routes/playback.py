from __future__ import annotations

import asyncio
import datetime as _dt
import random

import structlog

from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps
from tune_server.event_bus import Event, EventType
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

logger = structlog.get_logger()

router = APIRouter(prefix="/zones/{zone_id}", tags=["playback"])
global_router = APIRouter(tags=["playback"])

_sleep_timers: dict[int, asyncio.Task] = {}
_sleep_state: dict[int, dict] = {}  # zone_id -> {minutes, started_at, fading, original_volume}
_alarms: dict[int, dict] = {}

SLEEP_FADE_DURATION = 30  # seconds
SLEEP_FADE_STEPS = 30     # 1-second steps


def _clean_file_title(file_path: str) -> str:
    """Extract a clean title from a file path, avoiding raw UPnP IDs."""
    import re
    filename = file_path.rsplit("/", 1)[-1]
    # Remove extension
    name = filename.rsplit(".", 1)[0] if "." in filename else filename
    # UPnP IDs look like: d1234567890-coXXXXXXXX or just hex/numbers
    if re.match(r'^[a-f0-9d-]{20,}$', name, re.IGNORECASE):
        return "Piste audio"
    return filename


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

            try:
                resolved = await asyncio.wait_for(
                    asyncio.gather(*[resolve(t) for t in playlist_tracks], return_exceptions=True),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                logger.warning("resolve_tracks_timeout", type="streaming_playlist")
                resolved = []
            tracks = [t for t in resolved if not isinstance(t, Exception) and t.file_path]

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

            try:
                resolved = await asyncio.wait_for(
                    asyncio.gather(*[resolve_url(t) for t in album_tracks], return_exceptions=True),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                logger.warning("resolve_tracks_timeout", type="streaming_album")
                resolved = []
            tracks = [t for t in resolved if not isinstance(t, Exception) and t.file_path]

    elif request.source and request.source_id:
        # Streaming service track — resolve track metadata AND stream URL.
        # Invalidate any cached URL so a stopped/failed stream gets a fresh one.
        service = deps.streaming_services.get(request.source.value)
        if service and service.is_authenticated:
            if hasattr(service, '_url_cache'):
                service._url_cache.invalidate(request.source_id)
            track = await service.get_track(request.source_id)
            if track:
                url = await service.get_stream_url(request.source_id)
                if url:
                    track.file_path = url
                    tracks.append(track)

    elif request.file_path:
        # Direct URL playback (e.g. media server stream, podcast episode)
        from tune_server.models import Track, AudioFormat
        fmt = AudioFormat.MP3  # sensible default for HTTP streams (podcast, radio)
        url_lower = request.file_path.lower()
        if "flac" in url_lower:
            fmt = AudioFormat.FLAC
        elif "mp3" in url_lower:
            fmt = AudioFormat.MP3
        elif "aac" in url_lower or "m4a" in url_lower:
            fmt = AudioFormat.AAC
        elif "ogg" in url_lower:
            fmt = AudioFormat.OGG
        elif "wav" in url_lower:
            fmt = AudioFormat.WAV
        tracks.append(Track(
            id=None,
            title=request.title or _clean_file_title(request.file_path),
            artist_name=request.artist_name,
            album_title=request.album_title,
            cover_path=request.cover_path,
            duration_ms=request.duration_ms or 0,
            file_path=request.file_path,
            format=fmt,
        ))

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

    # If a start_index was supplied, clamp it to a valid range.
    start = 0
    if request.start_index is not None and tracks:
        start = max(0, min(request.start_index, len(tracks) - 1))

    # If zone is in a group, play on all group members
    group = deps.group_manager.get_group_for_zone(zone_id) if deps.group_manager else None
    try:
        if group and tracks:
            # Group play: keep prior single-track behaviour (no per-zone start
            # offset on group play yet); fall back to slicing.
            await group.play(tracks[start:] if start else tracks)
        elif tracks:
            await zone.player.play(tracks=tracks, start_position=start)
        else:
            # Resume current queue (no specific track requested)
            await zone.player.play()
    except Exception as e:
        logger.exception("play_route_error", zone_id=zone_id)
        raise HTTPException(status_code=502, detail=f"Playback error: {e}")

    return zone.to_model()


@router.post("/pause", response_model=Zone)
async def pause(zone_id: int):
    zone = _get_zone(zone_id)
    group = deps.group_manager.get_group_for_zone(zone_id) if deps.group_manager else None
    try:
        if group:
            await group.pause()
        else:
            await zone.player.pause()
    except Exception as e:
        logger.exception("pause_route_error", zone_id=zone_id)
        raise HTTPException(status_code=502, detail=f"Playback error: {e}")
    return zone.to_model()


@router.post("/resume", response_model=Zone)
async def resume(zone_id: int):
    zone = _get_zone(zone_id)
    group = deps.group_manager.get_group_for_zone(zone_id) if deps.group_manager else None
    try:
        if group:
            await group.resume()
        else:
            await zone.player.resume()
    except Exception as e:
        logger.exception("resume_route_error", zone_id=zone_id)
        raise HTTPException(status_code=502, detail=f"Playback error: {e}")
    return zone.to_model()


@router.post("/stop", response_model=Zone)
async def stop(zone_id: int):
    zone = _get_zone(zone_id)
    group = deps.group_manager.get_group_for_zone(zone_id) if deps.group_manager else None
    try:
        if group:
            await group.stop()
        else:
            await zone.player.stop()
    except Exception as e:
        logger.exception("stop_route_error", zone_id=zone_id)
        raise HTTPException(status_code=502, detail=f"Playback error: {e}")
    return zone.to_model()


@router.post("/next", response_model=Zone)
async def skip_next(zone_id: int):
    zone = _get_zone(zone_id)
    group = deps.group_manager.get_group_for_zone(zone_id) if deps.group_manager else None
    try:
        if group:
            await group.skip_next()
        else:
            await zone.player.skip_next()
    except Exception as e:
        logger.exception("skip_next_route_error", zone_id=zone_id)
        raise HTTPException(status_code=502, detail=f"Playback error: {e}")
    return zone.to_model()


@router.post("/previous", response_model=Zone)
async def skip_previous(zone_id: int):
    zone = _get_zone(zone_id)
    group = deps.group_manager.get_group_for_zone(zone_id) if deps.group_manager else None
    try:
        if group:
            await group.skip_previous()
        else:
            await zone.player.skip_previous()
    except Exception as e:
        logger.exception("skip_previous_route_error", zone_id=zone_id)
        raise HTTPException(status_code=502, detail=f"Playback error: {e}")
    return zone.to_model()


@router.post("/seek", response_model=Zone)
async def seek(zone_id: int, request: SeekRequest):
    zone = _get_zone(zone_id)
    track = zone.player.current_track
    if not track:
        raise HTTPException(status_code=409, detail="No track playing")
    position_ms = request.position_ms
    if track.duration_ms and position_ms > track.duration_ms:
        position_ms = track.duration_ms
    try:
        await zone.player.seek(position_ms)
    except Exception as e:
        logger.exception("seek_route_error", zone_id=zone_id)
        raise HTTPException(status_code=502, detail=f"Playback error: {e}")
    return zone.to_model()


@router.post("/volume", response_model=Zone)
async def set_volume(zone_id: int, request: VolumeRequest):
    zone = _get_zone(zone_id)
    try:
        await zone.player.set_volume(request.volume)
    except Exception as e:
        logger.exception("set_volume_route_error", zone_id=zone_id)
        raise HTTPException(status_code=502, detail=f"Playback error: {e}")
    return zone.to_model()


@router.get("/eq")
async def get_equalizer(zone_id: int):
    """Return current parametric EQ settings for a zone."""
    zone = _get_zone(zone_id)
    return zone.player.get_equalizer()


@router.post("/eq")
async def set_equalizer(zone_id: int, body: dict):
    """Set parametric EQ on a zone.

    Parametric mode (preferred):
        body: {
            "enabled": true,
            "bands": [
                {"freq": 60, "gain": 3, "q": 1.0},
                {"freq": 250, "gain": -2, "q": 1.0},
                ...
            ]
        }

    Preset mode (legacy shorthand):
        body: { "preset": "flat|bass_boost|treble_boost|vocal|rock|jazz|classical" }

    Each band becomes an FFmpeg ``equalizer`` filter applied during playback.
    Settings persist across tracks within the zone.
    """
    zone = _get_zone(zone_id)

    # ---- Preset shorthand (legacy compat) ----
    _PRESETS: dict[str, list[dict]] = {
        "flat": [],
        "bass_boost": [
            {"freq": 60, "gain": 6, "q": 1.0},
            {"freq": 125, "gain": 4, "q": 1.0},
            {"freq": 250, "gain": 2, "q": 1.0},
        ],
        "treble_boost": [
            {"freq": 8000, "gain": 4, "q": 1.0},
            {"freq": 12000, "gain": 5, "q": 1.0},
            {"freq": 16000, "gain": 6, "q": 1.0},
        ],
        "vocal": [
            {"freq": 1000, "gain": 3, "q": 1.0},
            {"freq": 2000, "gain": 4, "q": 1.0},
            {"freq": 4000, "gain": 3, "q": 1.0},
        ],
        "rock": [
            {"freq": 60, "gain": 4, "q": 1.0},
            {"freq": 125, "gain": 3, "q": 1.0},
            {"freq": 1000, "gain": -2, "q": 1.0},
            {"freq": 8000, "gain": 3, "q": 1.0},
            {"freq": 12000, "gain": 4, "q": 1.0},
        ],
        "jazz": [
            {"freq": 125, "gain": 2, "q": 1.0},
            {"freq": 250, "gain": 1, "q": 1.0},
            {"freq": 2000, "gain": 3, "q": 1.0},
            {"freq": 4000, "gain": 2, "q": 1.0},
            {"freq": 8000, "gain": 2, "q": 1.0},
        ],
        "classical": [
            {"freq": 60, "gain": -2, "q": 1.0},
            {"freq": 2000, "gain": 2, "q": 1.0},
            {"freq": 4000, "gain": 3, "q": 1.0},
            {"freq": 8000, "gain": 2, "q": 1.0},
            {"freq": 12000, "gain": 3, "q": 1.0},
        ],
    }

    preset = body.get("preset")
    if preset and preset in _PRESETS:
        bands = _PRESETS[preset]
        enabled = preset != "flat"
        zone.player.set_equalizer(enabled=enabled, bands=bands)
        return {"enabled": enabled, "bands": bands, "preset": preset}

    # ---- Parametric mode ----
    bands = body.get("bands", [])
    enabled = body.get("enabled", True)
    zone.player.set_equalizer(enabled=enabled, bands=bands)
    return {"enabled": enabled, "bands": bands}


@router.post("/dsp")
async def set_dsp(zone_id: int, body: dict = {}):
    """Set DSP effects (crossfeed, etc.).

    Crossfeed reduces stereo separation for more natural headphone listening
    by mixing a portion of each channel into the other.

    body: { "crossfeed": "light" | "medium" | "strong" | null }
    """
    zone = _get_zone(zone_id)

    crossfeed = body.get("crossfeed", None)  # None, "light", "medium", "strong"

    if crossfeed:
        levels = {"light": 0.15, "medium": 0.3, "strong": 0.45}
        mix = levels.get(crossfeed, 0.3)
        # Mix some of each channel into the other for a natural crossfeed effect
        filter_str = f"pan=stereo|c0={1-mix}*c0+{mix}*c1|c1={mix}*c0+{1-mix}*c1"
        zone.player.set_channel_filter(filter_str)
    else:
        zone.player.set_channel_filter(None)

    return {"crossfeed": crossfeed}


@router.get("/share")
async def share_now_playing(zone_id: int):
    """Generate a shareable 'Now Playing' card."""
    zone = _get_zone(zone_id)
    track = zone.current_track
    if not track:
        raise HTTPException(status_code=404, detail="Nothing playing")

    cover_url = None
    if track.cover_path:
        cover_url = f"/api/v1/library/artwork/{track.cover_path.split('/')[-1]}"

    return {
        "title": track.title,
        "artist": track.artist_name,
        "album": track.album_title,
        "cover_url": cover_url,
        "format": track.format.value if track.format else None,
        "source": track.source.value if track.source else "local",
        "text": f"🎵 {track.title} — {track.artist_name or 'Unknown'}\n💿 {track.album_title or ''}\n🎧 Tune Server",
    }


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
    elif request.file_path:
        from tune_server.models import Track, AudioFormat
        fmt = AudioFormat.MP3  # sensible default for HTTP streams
        url_lower = request.file_path.lower()
        if "flac" in url_lower:
            fmt = AudioFormat.FLAC
        elif "mp3" in url_lower:
            fmt = AudioFormat.MP3
        elif "aac" in url_lower or "m4a" in url_lower:
            fmt = AudioFormat.AAC
        elif "ogg" in url_lower:
            fmt = AudioFormat.OGG
        elif "wav" in url_lower:
            fmt = AudioFormat.WAV
        tracks.append(Track(
            id=None,
            title=request.title or _clean_file_title(request.file_path),
            artist_name=request.artist_name,
            album_title=request.album_title,
            cover_path=request.cover_path,
            duration_ms=request.duration_ms or 0,
            file_path=request.file_path,
            format=fmt,
        ))

    if tracks:
        zone.player.queue.add_tracks(tracks, position=request.position)
        # Notify clients
        await deps.event_bus.emit(Event(type=EventType.PLAYBACK_QUEUE_CHANGED, data={"zone_id": zone_id}))

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


@router.post("/transfer/{target_zone_id}")
async def transfer_playback(zone_id: int, target_zone_id: int):
    """Transfer current playback (track + queue + position) from one zone to another."""
    source = _get_zone(zone_id)
    target = deps.zone_manager.get_zone(target_zone_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target zone not found")

    # Copy queue
    source_queue = source.player.queue
    tracks = source_queue.tracks
    position = source_queue.position
    seek_ms = source.player.position_ms

    # Stop source
    await source.player.stop()

    # Load into target
    if tracks:
        target.player.queue.clear()
        target.player.queue.add_tracks(tracks)
        target.player.queue.jump_to(position)
        await target.player.play(seek_ms=seek_ms)

    await deps.event_bus.emit(Event(
        type=EventType.PLAYBACK_QUEUE_CHANGED,
        data={"zone_id": target_zone_id, "transferred_from": zone_id},
        source="playback",
    ))

    return target.to_model()


@router.post("/queue/clear", response_model=QueueLengthResponse)
async def clear_queue(zone_id: int):
    zone = _get_zone(zone_id)
    zone.player.queue.clear()
    await zone.player.stop()
    return QueueLengthResponse(queue_length=0)


@router.post("/sleep")
async def set_sleep_timer(zone_id: int, body: dict = {}):
    """Set a sleep timer with volume fade-out. Stops playback after `minutes`.

    Pass ``{"minutes": 0}`` to cancel. If cancelled during fade, volume is
    restored immediately.
    """
    zone = _get_zone(zone_id)

    minutes = body.get("minutes", 30)

    # Cancel existing timer and restore volume if fading
    if zone_id in _sleep_timers:
        _sleep_timers[zone_id].cancel()
        del _sleep_timers[zone_id]
    if zone_id in _sleep_state:
        state = _sleep_state.pop(zone_id)
        if state.get("fading") and state.get("original_volume") is not None:
            try:
                await zone.player.set_volume(state["original_volume"])
                logger.info("sleep_timer_cancelled_volume_restored",
                            zone_id=zone_id, volume=state["original_volume"])
            except Exception:
                logger.warning("sleep_timer_volume_restore_failed", zone_id=zone_id)

    if minutes <= 0:
        return {"sleep_timer": None, "zone_id": zone_id}

    started_at = asyncio.get_event_loop().time()
    _sleep_state[zone_id] = {
        "minutes": minutes,
        "started_at": started_at,
        "fading": False,
        "original_volume": None,
    }

    async def _sleep_with_fade():
        total_seconds = minutes * 60
        wait_seconds = max(0, total_seconds - SLEEP_FADE_DURATION)

        # Wait until fade should begin
        await asyncio.sleep(wait_seconds)

        # Begin fade-out
        original_volume = zone.player.volume
        if zone_id in _sleep_state:
            _sleep_state[zone_id]["fading"] = True
            _sleep_state[zone_id]["original_volume"] = original_volume

        logger.info("sleep_timer_fade_start", zone_id=zone_id,
                     from_volume=round(original_volume, 2))

        for step in range(1, SLEEP_FADE_STEPS + 1):
            # Check if timer was cancelled during fade
            if zone_id not in _sleep_state:
                return
            vol = original_volume * (1.0 - step / SLEEP_FADE_STEPS)
            try:
                await zone.player.set_volume(max(0.0, vol))
            except Exception:
                pass
            await asyncio.sleep(SLEEP_FADE_DURATION / SLEEP_FADE_STEPS)

        # Fade complete — stop playback
        try:
            await zone.player.stop()
        except Exception:
            logger.warning("sleep_timer_stop_failed", zone_id=zone_id)

        # Restore original volume setting so next play starts at normal level
        try:
            await zone.player.set_volume(original_volume)
        except Exception:
            pass

        _sleep_state.pop(zone_id, None)
        _sleep_timers.pop(zone_id, None)
        logger.info("sleep_timer_fired", zone_id=zone_id, minutes=minutes)

    _sleep_timers[zone_id] = asyncio.create_task(_sleep_with_fade())
    return {"sleep_timer": minutes, "zone_id": zone_id}


@router.get("/sleep")
async def get_sleep_timer(zone_id: int):
    """Get sleep timer status including remaining seconds and fade state."""
    _get_zone(zone_id)  # validate zone exists
    state = _sleep_state.get(zone_id)
    if state and zone_id in _sleep_timers and not _sleep_timers[zone_id].done():
        elapsed = asyncio.get_event_loop().time() - state["started_at"]
        total = state["minutes"] * 60
        remaining = max(0, int(total - elapsed))
        return {
            "active": True,
            "remaining_seconds": remaining,
            "fading": state.get("fading", False),
            "zone_id": zone_id,
        }
    return {"active": False, "remaining_seconds": 0, "fading": False, "zone_id": zone_id}


@router.post("/queue/save-as-playlist")
async def save_queue_as_playlist(zone_id: int, body: dict = {}):
    """Save the current queue as a new playlist."""
    zone = _get_zone(zone_id)

    name = body.get("name", f"Queue {zone.name}")
    tracks = zone.player.queue.tracks
    if not tracks:
        raise HTTPException(400, "Queue is empty")

    # Create playlist
    playlist_id = await deps.playlist_repo.create(name)

    # Add tracks
    track_ids = [t.id for t in tracks if t.id]
    if track_ids:
        await deps.playlist_repo.add_tracks(playlist_id, track_ids)

    return {"playlist_id": playlist_id, "name": name, "track_count": len(track_ids)}


@router.get("/audiophile")
async def get_audiophile_mode(zone_id: int):
    """Return current audiophile mode status for a zone."""
    zone = _get_zone(zone_id)
    effects_disabled = []
    if zone.player.audiophile_mode:
        effects_disabled = ["eq", "crossfade", "normalization"]
    return {"enabled": zone.player.audiophile_mode, "effects_disabled": effects_disabled}


@router.post("/audiophile")
async def set_audiophile_mode(zone_id: int, body: dict = {}):
    """Enable or disable audiophile mode on a zone.

    When enabled: disables EQ, crossfade, normalization, sets volume to 1.0.
    When disabled: only clears the flag (user controls DSP settings separately).
    """
    zone = _get_zone(zone_id)
    enabled = body.get("enabled", True)
    await zone.player.set_audiophile_mode(enabled)
    effects_disabled = ["eq", "crossfade", "normalization"] if enabled else []
    return {"enabled": enabled, "effects_disabled": effects_disabled}


@router.get("/quality")
async def get_quality_preference(zone_id: int):
    """Return current streaming quality preference for a zone."""
    zone = _get_zone(zone_id)
    return {"quality": zone.player.quality_preference}


@router.post("/quality")
async def set_quality_preference(zone_id: int, body: dict = {}):
    """Set streaming quality preference for a zone.

    Accepted values: "max", "hires", "cd", "low".
    """
    zone = _get_zone(zone_id)
    quality = body.get("quality", "max")
    if quality not in ("max", "hires", "cd", "low"):
        raise HTTPException(status_code=400, detail="quality must be one of: max, hires, cd, low")
    zone.player.set_quality_preference(quality)
    return {"quality": quality}


@router.post("/crossfade")
async def set_crossfade(zone_id: int, body: dict = {}):
    """Enable/disable crossfade between tracks."""
    zone = _get_zone(zone_id)
    enabled = body.get("enabled", True)
    duration = body.get("duration", 3.0)
    zone.player._crossfade_enabled = enabled
    zone.player._crossfade_duration = duration
    return {"crossfade_enabled": enabled, "crossfade_duration": duration}


@router.post("/normalization")
async def set_normalization(zone_id: int, body: dict = {}):
    """Enable/disable volume normalization."""
    zone = _get_zone(zone_id)
    enabled = body.get("enabled", True)
    target_lufs = body.get("target_lufs", -14.0)
    zone.player._normalization_enabled = enabled
    zone.player._normalization_target = target_lufs
    return {"normalization_enabled": enabled, "target_lufs": target_lufs}


@router.get("/status", response_model=Zone)
async def get_status(zone_id: int):
    zone = _get_zone(zone_id)
    return zone.to_model()


# --- Alarm Clock ---


@router.post("/alarm")
async def set_alarm(zone_id: int, body: dict = {}):
    """Set a musical alarm. Plays at the given time with fade-in."""
    zone = _get_zone(zone_id)

    time_str = body.get("time")  # "07:30"
    if not time_str:
        # Cancel alarm
        if zone_id in _alarms:
            task = _alarms[zone_id].get("task")
            if task:
                task.cancel()
            del _alarms[zone_id]
        return {"alarm": None}

    playlist_id = body.get("playlist_id")
    radio_id = body.get("radio_id")
    album_id = body.get("album_id")
    fade_seconds = body.get("fade_seconds", 30)
    volume = body.get("volume", 0.5)

    # Parse time
    hour, minute = map(int, time_str.split(":"))

    # Calculate seconds until alarm
    now = _dt.datetime.now()
    alarm_time = now.replace(hour=hour, minute=minute, second=0)
    if alarm_time <= now:
        alarm_time += _dt.timedelta(days=1)
    delay = (alarm_time - now).total_seconds()

    # Cancel existing
    if zone_id in _alarms:
        task = _alarms[zone_id].get("task")
        if task:
            task.cancel()

    async def _alarm_fire():
        await asyncio.sleep(delay)
        # Start at volume 0, fade in
        await zone.player.set_volume(0.0)

        if album_id:
            tracks = await deps.track_repo.list_by_album(album_id)
            if tracks:
                await zone.player.play(tracks=tracks)
        elif playlist_id:
            playlist_tracks = await deps.playlist_repo.get_tracks(playlist_id)
            if playlist_tracks:
                await zone.player.play(tracks=playlist_tracks)
        elif radio_id:
            radio = await deps.radio_repo.get(radio_id)
            if radio:
                from tune_server.models import Track, Source, AudioFormat
                radio_track = Track(title=radio.name, file_path=radio.stream_url, source=Source.RADIO)
                await zone.player.play(tracks=[radio_track])

        # Fade in
        steps = max(1, int(fade_seconds * 2))
        for i in range(steps + 1):
            vol = (i / steps) * volume
            await zone.player.set_volume(vol)
            await asyncio.sleep(fade_seconds / steps)

        if zone_id in _alarms:
            del _alarms[zone_id]

    task = asyncio.create_task(_alarm_fire())
    _alarms[zone_id] = {
        "task": task,
        "time": time_str,
        "fade_seconds": fade_seconds,
        "alarm_time": alarm_time.isoformat(),
    }

    return {"alarm": time_str, "fires_in_seconds": int(delay), "fade_seconds": fade_seconds}


@router.get("/alarm")
async def get_alarm(zone_id: int):
    """Get alarm status."""
    if zone_id in _alarms:
        info = _alarms[zone_id]
        return {"active": True, "time": info["time"], "alarm_time": info.get("alarm_time")}
    return {"active": False}


@router.delete("/alarm")
async def cancel_alarm(zone_id: int):
    """Cancel alarm."""
    if zone_id in _alarms:
        task = _alarms[zone_id].get("task")
        if task:
            task.cancel()
        del _alarms[zone_id]
    return {"cancelled": True}


# --- Global endpoints (no zone_id) ---


@global_router.get("/zones/now-listening")
async def now_listening():
    """What's playing right now across all zones."""
    if not deps.zone_manager:
        return []

    result = []
    for zone in deps.zone_manager.list_zones():
        if zone.player.state.value in ("playing", "paused"):
            track = zone.current_track
            if track:
                result.append({
                    "zone_id": zone.zone_id,
                    "zone_name": zone.name,
                    "state": zone.player.state.value,
                    "track": {
                        "title": track.title,
                        "artist": track.artist_name,
                        "album": track.album_title,
                        "cover_path": track.cover_path,
                        "duration_ms": track.duration_ms,
                    },
                    "position_ms": zone.player.position_ms,
                    "volume": zone.player.volume,
                })

    return result


@global_router.get("/widget/data")
async def widget_data():
    """Compact data for mobile widgets -- current track + controls."""
    if not deps.zone_manager:
        return {"playing": False}

    # Find the first playing zone
    active = None
    for zone in deps.zone_manager.list_zones():
        if zone.player.state.value == "playing":
            active = zone
            break

    if not active:
        # Try paused
        for zone in deps.zone_manager.list_zones():
            if zone.player.state.value == "paused":
                active = zone
                break

    if not active or not active.current_track:
        return {"playing": False}

    track = active.current_track
    return {
        "playing": active.player.state.value == "playing",
        "zone_id": active.zone_id,
        "zone_name": active.name,
        "title": track.title,
        "artist": track.artist_name,
        "album": track.album_title,
        "cover_url": f"/api/v1/library/artwork/{track.cover_path.split('/')[-1]}" if track.cover_path and not track.cover_path.startswith("http") else track.cover_path,
        "position_ms": active.player.position_ms,
        "duration_ms": track.duration_ms,
        "volume": active.player.volume,
    }


@global_router.post("/playback/shuffle-all")
async def shuffle_all_library(zone_id: int):
    """Shuffle-play the entire local library (up to 5 000 tracks)."""
    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    if not deps.track_repo:
        raise HTTPException(status_code=503, detail="Library not available")

    tracks = await deps.track_repo.list_random(limit=5000)
    if not tracks:
        raise HTTPException(status_code=404, detail="Library is empty")

    # Shuffle in-place for good measure (DB RANDOM() is already random,
    # but this also works for repos that don't support ORDER BY RANDOM).
    random.shuffle(tracks)

    try:
        await zone.player.play(tracks=tracks, start_position=0)
    except Exception as e:
        logger.exception("shuffle_all_error", zone_id=zone_id)
        raise HTTPException(status_code=502, detail=f"Playback error: {e}")

    await deps.event_bus.emit(Event(
        type=EventType.PLAYBACK_QUEUE_CHANGED,
        data={"zone_id": zone_id},
        source="playback",
    ))

    return {"status": "ok", "track_count": len(tracks)}


# --- Zone Audio Profiles ---


@router.get("/audio-profile")
async def get_audio_profile(zone_id: int):
    row = await deps.db.fetchone(
        "SELECT * FROM zone_audio_profiles WHERE zone_id = ?", (zone_id,))
    if row:
        return dict(row)
    return {"zone_id": zone_id, "name": "Default", "eq_preset": None, "bass_boost": 0, "treble_boost": 0}


@router.post("/audio-profile")
async def set_audio_profile(zone_id: int, body: dict = {}):
    name = body.get("name", "Default")
    eq_preset = body.get("eq_preset")
    bass_boost = body.get("bass_boost", 0)
    treble_boost = body.get("treble_boost", 0)
    loudness_comp = body.get("loudness_compensation", False)
    crossfeed = body.get("crossfeed")

    await deps.db.execute(
        """INSERT INTO zone_audio_profiles (zone_id, name, eq_preset, bass_boost, treble_boost, loudness_compensation, crossfeed)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(zone_id, name) DO UPDATE SET eq_preset=?, bass_boost=?, treble_boost=?, loudness_compensation=?, crossfeed=?""",
        (zone_id, name, eq_preset, bass_boost, treble_boost, loudness_comp, crossfeed,
         eq_preset, bass_boost, treble_boost, loudness_comp, crossfeed))
    await deps.db.commit()

    return {"zone_id": zone_id, "name": name, "eq_preset": eq_preset, "bass_boost": bass_boost, "treble_boost": treble_boost}

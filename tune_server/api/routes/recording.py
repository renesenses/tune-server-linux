"""Recording API routes."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps
from tune_server.models import Source, Track

logger = structlog.get_logger()


async def _enrich_track(track: Track) -> None:
    """Fill in missing album_title/cover from the streaming connector."""
    if track.album_title and track.cover_path:
        return  # already complete
    if not track.source_id or not track.source:
        return

    svc_name = track.source.value if isinstance(track.source, Source) else str(track.source)
    svc = deps.streaming_services.get(svc_name)
    if not svc:
        return

    try:
        full = await svc.get_track(track.source_id)
        if full:
            if not track.album_title and full.album_title:
                track.album_title = full.album_title
            if not track.cover_path and full.cover_path:
                track.cover_path = full.cover_path
            if not track.artist_name and full.artist_name:
                track.artist_name = full.artist_name
    except Exception:
        logger.debug("enrich_track_failed", source=svc_name, source_id=track.source_id)

router = APIRouter(prefix="/zones/{zone_id}/record", tags=["recording"])


@router.post("/start")
async def start_recording(zone_id: int):
    """Start recording for a zone."""
    if not deps.recording_service:
        raise HTTPException(status_code=503, detail="Recording service not available")

    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    deps.recording_service.start_recording_zone(zone_id)

    # If already playing, capture the current track immediately
    track = zone.player.current_track
    if track:
        # Enrich track metadata from streaming service if missing
        await _enrich_track(track)

        # For streaming tracks, resolve URL if not already set
        if not track.file_path and track.source_id and zone.player._stream_url_resolver:
            try:
                import asyncio
                url = await asyncio.wait_for(
                    zone.player._stream_url_resolver(track), timeout=15
                )
                if url:
                    track.file_path = url
            except Exception:
                pass
        deps.recording_service.set_track_info(zone_id, track)
        await deps.recording_service.capture_current_track(zone_id)

    return {"status": "recording", "zone_id": zone_id}


@router.post("/stop")
async def stop_recording(zone_id: int):
    """Stop recording for a zone."""
    if not deps.recording_service:
        raise HTTPException(status_code=503, detail="Recording service not available")

    session = await deps.recording_service.stop_recording(zone_id)
    if session:
        return session.to_dict()
    return {"status": "idle", "zone_id": zone_id}


@router.get("/status")
async def recording_status(zone_id: int):
    """Get recording status for a zone."""
    if not deps.recording_service:
        return {"state": "unavailable"}

    session = deps.recording_service.get_session(zone_id)
    if session:
        return session.to_dict()
    return {
        "zone_id": zone_id,
        "state": "recording" if deps.recording_service.is_recording(zone_id) else "idle",
    }


# Non-zone-specific endpoints
recordings_router = APIRouter(prefix="/recordings", tags=["recording"])


@recordings_router.get("")
async def list_recordings():
    """List all recorded files."""
    if not deps.recording_service:
        return []
    return deps.recording_service.list_recordings()


@recordings_router.delete("/{path:path}")
async def delete_recording(path: str):
    """Delete a recorded file."""
    if not deps.recording_service:
        raise HTTPException(status_code=503, detail="Recording service not available")

    from pathlib import Path
    full_path = deps.recording_service._output_dir / path
    if full_path.exists() and full_path.is_file():
        full_path.unlink()
        return {"deleted": path}
    raise HTTPException(status_code=404, detail="Recording not found")

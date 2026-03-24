"""Recording API routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps

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

    # If already playing, capture the current track
    track = zone.player.current_track
    if track:
        deps.recording_service.set_track_info(zone_id, track)

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

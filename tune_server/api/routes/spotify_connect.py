from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from tune_server.api.deps import deps

router = APIRouter(prefix="/spotify-connect", tags=["spotify-connect"])


class EnableRequest(BaseModel):
    zone_id: int
    device_name: str | None = Field(None, description="Override device name (default: hostname-based)")


@router.get("/status")
async def status() -> dict:
    mgr = deps.spotify_connect
    if mgr is None:
        return {"enabled": False, "available": False, "reason": "manager_not_initialized"}
    return {**mgr.status, "available": True}


@router.post("/enable")
async def enable(req: EnableRequest) -> dict:
    mgr = deps.spotify_connect
    if mgr is None:
        raise HTTPException(503, "Spotify Connect manager not initialized")
    if not mgr._binary_available():
        raise HTTPException(503, "librespot binary not found on this system")
    await mgr.enable(zone_id=req.zone_id, device_name=req.device_name)
    return mgr.status


@router.post("/disable")
async def disable() -> dict:
    mgr = deps.spotify_connect
    if mgr is None:
        raise HTTPException(503, "Spotify Connect manager not initialized")
    await mgr.disable()
    return mgr.status

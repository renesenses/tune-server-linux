from __future__ import annotations

from fastapi import APIRouter

from tune_server.api.deps import deps
from tune_server.models import DiscoveredDevice

router = APIRouter(prefix="/devices", tags=["devices"])


@router.get("", response_model=list[DiscoveredDevice])
async def list_devices():
    if not deps.discovery_manager:
        return []
    return deps.discovery_manager.list_devices()


@router.get("/{device_id}", response_model=DiscoveredDevice)
async def get_device(device_id: str):
    if not deps.discovery_manager:
        return {"error": "Discovery not available"}
    device = deps.discovery_manager.get_device(device_id)
    if not device:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Device not found")
    return device

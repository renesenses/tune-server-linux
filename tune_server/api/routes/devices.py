from __future__ import annotations

from fastapi import APIRouter

from tune_server.api.deps import deps
from tune_server.models import DiscoveredDevice, LocalAudioDevice

router = APIRouter(prefix="/devices", tags=["devices"])


@router.get("", response_model=list[DiscoveredDevice])
async def list_devices():
    if not deps.discovery_manager:
        return []
    return deps.discovery_manager.list_devices()


@router.get("/audio", response_model=list[LocalAudioDevice])
async def list_audio_devices():
    import sounddevice as sd

    result = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            result.append(
                LocalAudioDevice(
                    id=str(i),
                    name=d["name"],
                    channels=d["max_output_channels"],
                    sample_rate=int(d["default_samplerate"]),
                )
            )
    return result


@router.get("/{device_id}", response_model=DiscoveredDevice)
async def get_device(device_id: str):
    if not deps.discovery_manager:
        return {"error": "Discovery not available"}
    device = deps.discovery_manager.get_device(device_id)
    if not device:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Device not found")
    return device

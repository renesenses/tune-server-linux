from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tune_server.api.deps import deps
from tune_server.models import DiscoveredDevice, LocalAudioDevice, OutputType

router = APIRouter(prefix="/devices", tags=["devices"])

# In-memory pairing sessions: device_id -> pyatv pairing object
_pairing_sessions: dict[str, object] = {}


class PairPinRequest(BaseModel):
    pin: str


class PairResponse(BaseModel):
    status: str
    device_id: str
    message: str | None = None


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
        raise HTTPException(status_code=503, detail="Discovery not available")
    device = deps.discovery_manager.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@router.post("/{device_id}/pair", response_model=PairResponse)
async def begin_pairing(device_id: str):
    """Begin AirPlay pairing — the Apple TV will display a PIN code."""
    if not deps.discovery_manager or not deps.discovery_manager.mdns:
        raise HTTPException(status_code=503, detail="mDNS discovery not available")

    device = deps.discovery_manager.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.type != OutputType.AIRPLAY:
        raise HTTPException(status_code=400, detail="Pairing only supported for AirPlay devices")

    config = deps.discovery_manager.mdns.get_atv_config(device_id)
    if not config:
        raise HTTPException(status_code=404, detail="AirPlay config not found")

    try:
        import pyatv

        # Close any existing pairing session for this device
        if device_id in _pairing_sessions:
            try:
                await _pairing_sessions[device_id].close()
            except Exception:
                pass
            del _pairing_sessions[device_id]

        # Find the best protocol to pair with
        pairing_protocol = None
        for protocol in [pyatv.Protocol.AirPlay, pyatv.Protocol.Companion, pyatv.Protocol.RAOP]:
            if config.get_service(protocol) is not None:
                pairing_protocol = protocol
                break

        if not pairing_protocol:
            raise HTTPException(status_code=400, detail="No pairable protocol found on device")

        pairing = await pyatv.pair(config, pairing_protocol, asyncio.get_running_loop())
        await pairing.begin()

        _pairing_sessions[device_id] = pairing
        return PairResponse(
            status="awaiting_pin",
            device_id=device_id,
            message=f"Enter the PIN shown on {device.name}",
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pairing failed: {e}")


@router.post("/{device_id}/pair/pin", response_model=PairResponse)
async def submit_pairing_pin(device_id: str, req: PairPinRequest):
    """Submit the PIN code displayed on the Apple TV to complete pairing."""
    if device_id not in _pairing_sessions:
        raise HTTPException(status_code=400, detail="No active pairing session. Call POST /pair first.")

    pairing = _pairing_sessions[device_id]

    try:
        pairing.pin(int(req.pin))
        await pairing.finish()

        if pairing.has_paired:
            # Save credentials to DB
            credentials = pairing.service.credentials
            if deps.db and credentials:
                device = deps.discovery_manager.get_device(device_id) if deps.discovery_manager else None
                name = device.name if device else device_id
                await deps.db.execute(
                    "INSERT OR REPLACE INTO device_credentials (device_id, device_name, credentials) VALUES (?, ?, ?)",
                    (device_id, name, credentials),
                )
                await deps.db.commit()

            await pairing.close()
            del _pairing_sessions[device_id]

            return PairResponse(
                status="paired",
                device_id=device_id,
                message="Pairing successful! Credentials saved.",
            )
        else:
            await pairing.close()
            del _pairing_sessions[device_id]
            raise HTTPException(status_code=400, detail="Pairing failed — wrong PIN?")

    except HTTPException:
        raise
    except Exception as e:
        if device_id in _pairing_sessions:
            try:
                await _pairing_sessions[device_id].close()
            except Exception:
                pass
            del _pairing_sessions[device_id]
        raise HTTPException(status_code=500, detail=f"Pairing error: {e}")

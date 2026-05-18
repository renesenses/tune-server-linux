from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from tune_server.api.deps import deps
from tune_server.models import DiscoveredDevice, LocalAudioDevice, OutputType
from tune_server.outputs.dlna_buffer_stats import (
    DEFAULT_BUFFER_S,
    MAX_BUFFER_S,
    MIN_BUFFER_S,
    dlna_buffer_registry,
)

logger = structlog.get_logger()

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
    """List all discovered network devices plus local audio outputs.

    Local audio devices (USB DACs, soundcards) are included with
    type='local' so they appear alongside DLNA/AirPlay devices in the
    UI device list.  On Windows, the sounddevice list is refreshed on
    every call to pick up hot-plugged USB DACs.
    """
    network: list[DiscoveredDevice] = []
    if deps.discovery_manager:
        network = deps.discovery_manager.list_devices_deduped()

    # Include local audio devices so they show up in the UI device picker
    local_devs = _query_local_audio_devices()
    for ld in local_devs:
        network.append(DiscoveredDevice(
            id=f"local:{ld.name}",
            name=ld.name,
            type=OutputType.LOCAL,
            host="127.0.0.1",
            port=0,
            available=True,
            capabilities={
                "channels": ld.channels,
                "sample_rate": ld.sample_rate,
                "is_default": ld.is_default,
            },
        ))

    return network


def _query_local_audio_devices() -> list[LocalAudioDevice]:
    """Query sounddevice for local audio outputs, filtering virtual devices.

    Called on every request so hot-plugged USB DACs appear immediately
    (important on Windows where DACs are frequently connected/disconnected).
    """
    try:
        import sounddevice as sd
    except Exception:
        return []

    # ALSA aliases and virtual devices to hide
    _ALSA_ALIASES = {
        "sysdefault", "default", "front", "surround40", "surround51",
        "surround71", "iec958", "spdif", "dmix", "dsnoop",
        "plug", "pulse", "pipewire",
    }

    try:
        default_output = sd.default.device[1]  # default output device index
    except Exception:
        default_output = -1

    result: list[LocalAudioDevice] = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0:
                name = d["name"]
                # Skip ALSA aliases (Linux) — keep only real hardware devices
                if name.lower() in _ALSA_ALIASES:
                    continue
                # Skip channels > 32 (virtual mixers like sysdefault)
                if d["max_output_channels"] > 32:
                    continue
                result.append(
                    LocalAudioDevice(
                        id=str(i),
                        name=name,
                        channels=d["max_output_channels"],
                        sample_rate=int(d["default_samplerate"]),
                        is_default=(i == default_output),
                    )
                )
    except Exception:
        logger.exception("query_local_audio_devices_error")
    return result


@router.get("/audio", response_model=list[LocalAudioDevice])
async def list_audio_devices():
    """List local audio output devices (USB DACs, soundcards).

    Refreshed on every call to detect hot-plugged devices.
    """
    return _query_local_audio_devices()


@router.post("/scan", response_model=list[DiscoveredDevice])
async def scan_devices():
    """Force an immediate rescan of all LAN devices (DLNA + AirPlay)."""
    if not deps.discovery_manager:
        return []
    return await deps.discovery_manager.rescan()


@router.post("/clear")
async def clear_devices():
    """Remove all discovered devices from the list."""
    if not deps.discovery_manager:
        return {"cleared": 0}
    count = len(deps.discovery_manager.list_devices())
    if deps.discovery_manager.ssdp:
        deps.discovery_manager.ssdp._devices.clear()
        deps.discovery_manager.ssdp._dmr_devices.clear()
    if deps.discovery_manager.mdns:
        deps.discovery_manager.mdns._devices.clear()
        deps.discovery_manager.mdns._atv_configs.clear()
    return {"cleared": count}


@router.get("/buffer-stats/all")
async def get_all_buffer_stats():
    """Show buffer stability metrics for all tracked DLNA devices."""
    return dlna_buffer_registry.all_stats()


@router.get("/{device_id}", response_model=DiscoveredDevice)
async def get_device(device_id: str):
    # Handle local audio devices (id starts with "local:")
    if device_id.startswith("local:"):
        dev_name = device_id[6:]
        for ld in _query_local_audio_devices():
            if ld.name == dev_name:
                return DiscoveredDevice(
                    id=device_id,
                    name=ld.name,
                    type=OutputType.LOCAL,
                    host="127.0.0.1",
                    port=0,
                    available=True,
                    capabilities={
                        "channels": ld.channels,
                        "sample_rate": ld.sample_rate,
                        "is_default": ld.is_default,
                    },
                )
        raise HTTPException(status_code=404, detail="Local audio device not found")

    if not deps.discovery_manager:
        raise HTTPException(status_code=503, detail="Discovery not available")
    device = deps.discovery_manager.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@router.delete("/{device_id}")
async def delete_device(device_id: str):
    """Remove a single discovered device from the list."""
    if not deps.discovery_manager:
        raise HTTPException(status_code=503, detail="Discovery not available")
    removed = False
    if deps.discovery_manager.ssdp:
        if device_id in deps.discovery_manager.ssdp._devices:
            del deps.discovery_manager.ssdp._devices[device_id]
            deps.discovery_manager.ssdp._dmr_devices.pop(device_id, None)
            removed = True
    if deps.discovery_manager.mdns:
        if device_id in deps.discovery_manager.mdns._devices:
            del deps.discovery_manager.mdns._devices[device_id]
            deps.discovery_manager.mdns._atv_configs.pop(device_id, None)
            removed = True
    if not removed:
        raise HTTPException(status_code=404, detail="Device not found")
    return {"deleted": device_id}


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
            credentials = None
            try:
                credentials = pairing.service.credentials
            except Exception:
                pass
            logger.info("airplay_pairing_done", device_id=device_id, has_credentials=bool(credentials))

            if deps.db and credentials:
                device = deps.discovery_manager.get_device(device_id) if deps.discovery_manager else None
                name = device.name if device else device_id
                await deps.db.execute(
                    "INSERT INTO device_credentials (device_id, device_name, credentials) VALUES (?, ?, ?) "
                    "ON CONFLICT (device_id) DO UPDATE SET device_name = EXCLUDED.device_name, credentials = EXCLUDED.credentials",
                    (device_id, name, str(credentials)),
                )
                await deps.db.commit()
                logger.info("airplay_credentials_saved", device_id=device_id)

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


# --- Adaptive DLNA buffer sizing ---


class BufferPatchRequest(BaseModel):
    buffer_s: float = Field(
        ..., ge=MIN_BUFFER_S, le=MAX_BUFFER_S,
        description=f"Buffer size in seconds ({MIN_BUFFER_S}-{MAX_BUFFER_S})",
    )
    auto: bool = Field(
        default=False,
        description="If true, clear manual override and re-enable auto-adjustment",
    )


@router.get("/{device_id}/buffer-stats")
async def get_device_buffer_stats(device_id: str):
    """Show per-device DLNA buffer stability metrics.

    Returns buffer size, event counts, and auto-adjustment state.
    """
    stats = dlna_buffer_registry.get(device_id)
    if not stats:
        # Device exists but has no buffer stats yet — return defaults
        if deps.discovery_manager:
            device = deps.discovery_manager.get_device(device_id)
            if device:
                return {
                    "device_id": device_id,
                    "device_name": device.name,
                    "buffer_s": DEFAULT_BUFFER_S,
                    "manual_override": False,
                    "total_disconnections": 0,
                    "total_underruns": 0,
                    "total_interruptions": 0,
                    "total_adjustments": 0,
                    "recent_events_in_window": 0,
                    "window_minutes": 10,
                }
        raise HTTPException(status_code=404, detail="Device not found or no buffer stats available")
    return stats.to_dict()


@router.patch("/{device_id}/buffer")
async def set_device_buffer(device_id: str, req: BufferPatchRequest):
    """Manually set the buffer size for a DLNA device.

    Set auto=true to clear the manual override and re-enable auto-adjustment.
    """
    stats = dlna_buffer_registry.get_or_create(device_id)
    # Try to populate device name if available
    if not stats.device_name and deps.discovery_manager:
        device = deps.discovery_manager.get_device(device_id)
        if device:
            stats.device_name = device.name

    if req.auto:
        stats.clear_manual_override()
        return {
            "device_id": device_id,
            "buffer_s": stats.buffer_s,
            "manual_override": False,
            "message": "Auto-adjustment re-enabled",
        }

    stats.set_manual_buffer(req.buffer_s)
    return {
        "device_id": device_id,
        "buffer_s": stats.buffer_s,
        "manual_override": True,
        "message": f"Buffer set to {stats.buffer_s}s (manual override)",
    }

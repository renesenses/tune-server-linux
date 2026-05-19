from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps
from tune_server.config import settings
from tune_server.models import Zone, ZoneCreateRequest, ZoneUpdateRequest, ZoneGroupRequest, ZoneGroupResponse, StereoPairRequest, StereoPairResponse, SurroundGroupRequest, SurroundGroupResponse

logger = structlog.get_logger()

router = APIRouter(prefix="/zones", tags=["zones"])


@router.get("", response_model=list[Zone])
async def list_zones():
    """List all configured zones. Default zone (TUNE_DEFAULT_ZONE_ID) is returned first."""
    zones = deps.zone_manager.list_zones()
    models = [z.to_model() for z in zones]
    default_id = settings.default_zone_id
    if default_id:
        models.sort(key=lambda z: (0 if z.id == default_id else 1, z.id or 0))
    return models


@router.post("", response_model=Zone, status_code=201)
async def create_zone(request: ZoneCreateRequest):
    """Create a new zone with the given output type and settings."""
    try:
        zone = await deps.zone_manager.create_zone(
            name=request.name,
            output_type=request.output_type,
            output_device_id=request.output_device_id,
            sync_delay_ms=request.sync_delay_ms,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return zone.to_model()


@router.get("/{zone_id}", response_model=Zone)
async def get_zone(zone_id: int):
    """Get a single zone by ID."""
    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")
    return zone.to_model()


@router.put("/{zone_id}", response_model=Zone)
async def update_zone(zone_id: int, request: ZoneUpdateRequest):
    """Full update of a zone (name and/or sync delay)."""
    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")
    try:
        zone = await deps.zone_manager.update_zone(
            zone_id,
            name=request.name,
            sync_delay_ms=request.sync_delay_ms,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Zone not found")
    return zone.to_model()


@router.patch("/{zone_id}", response_model=Zone)
async def patch_zone(zone_id: int, request: ZoneUpdateRequest):
    """Partial update of a zone (only provided fields are changed).

    Supports changing name, sync_delay_ms, and output device.
    When output_type or output_device_id is provided, the zone's output
    is hot-swapped: playback stops, the old output is disconnected, and
    the new output is connected. Queue, settings, and volume are preserved.
    """
    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    # Handle output change if requested
    if request.output_type is not None or request.output_device_id is not None:
        new_output_type = request.output_type or zone.output_type
        new_device_id = request.output_device_id
        try:
            # Stop playback before swapping output
            try:
                await zone.player.stop()
            except Exception:
                pass
            await deps.zone_manager.set_output(
                zone_id, new_output_type, new_device_id,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Zone not found")
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))

    # Handle name / sync_delay_ms updates
    if request.name is not None or request.sync_delay_ms is not None:
        try:
            zone = await deps.zone_manager.update_zone(
                zone_id,
                name=request.name,
                sync_delay_ms=request.sync_delay_ms,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Zone not found")

    # Re-fetch after all mutations
    zone = deps.zone_manager.get_zone(zone_id)
    return zone.to_model()


@router.delete("/{zone_id}", status_code=204)
async def delete_zone(zone_id: int):
    """Delete a zone and release its output."""
    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")
    await deps.zone_manager.delete_zone(zone_id)


@router.post("/stereo-pair", response_model=StereoPairResponse, status_code=201)
async def create_stereo_pair(request: StereoPairRequest):
    """Create a stereo pair from two DLNA devices."""
    try:
        pair_id = await deps.zone_manager.create_stereo_pair(
            name=request.name,
            left_device_id=request.left_device_id,
            right_device_id=request.right_device_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Find the created zone IDs
    pairs = deps.zone_manager.get_stereo_pairs()
    for p in pairs:
        if p["stereo_pair_id"] == pair_id:
            left_id = p["left_zone"].id if p["left_zone"] else 0
            right_id = p["right_zone"].id if p["right_zone"] else 0
            return StereoPairResponse(
                stereo_pair_id=pair_id,
                left_zone_id=left_id,
                right_zone_id=right_id,
            )
    return StereoPairResponse(stereo_pair_id=pair_id, left_zone_id=0, right_zone_id=0)


@router.delete("/stereo-pair/{pair_id}", status_code=204)
async def dissolve_stereo_pair(pair_id: str):
    """Dissolve a stereo pair and delete both zones."""
    try:
        await deps.zone_manager.dissolve_stereo_pair(pair_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Stereo pair not found")


@router.get("/stereo-pairs/list")
async def list_stereo_pairs():
    """List all active stereo pairs."""
    return deps.zone_manager.get_stereo_pairs()


@router.post("/surround", response_model=SurroundGroupResponse, status_code=201)
async def create_surround_group(request: SurroundGroupRequest):
    """Create a surround group from N DLNA devices mapped to channels."""
    try:
        result = await deps.zone_manager.create_surround_group(
            name=request.name,
            channel_map=request.channel_map,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return SurroundGroupResponse(**result)


@router.delete("/surround/{group_id}", status_code=204)
async def dissolve_surround_group(group_id: str):
    """Dissolve a surround group and delete all its zones."""
    try:
        await deps.zone_manager.dissolve_surround_group(group_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Surround group not found")


@router.get("/surround/list")
async def list_surround_groups():
    """List all active surround groups."""
    return deps.zone_manager.get_surround_groups()


@router.put("/surround/{group_id}/calibrate")
async def calibrate_surround_group(group_id: str, delays: dict):
    """Set per-zone delay offsets for time alignment.

    Body: {"zone_id": delay_ms, ...} e.g. {"3": 15, "5": 0, "7": 8}
    """
    groups = deps.zone_manager.get_surround_groups()
    group = next((g for g in groups if g["surround_group_id"] == group_id), None)
    if not group:
        raise HTTPException(status_code=404, detail="Surround group not found")
    updated = {}
    for z_info in group["zones"]:
        zid = z_info["zone_id"]
        delay = delays.get(str(zid))
        if delay is not None:
            zone = deps.zone_manager.get_zone(zid)
            if zone:
                zone.sync_delay_ms = int(delay)
                updated[zid] = int(delay)
    return {"calibrated": updated}


def _resolve_zone_brand_key(output_device_id: str | None) -> str:
    """Return a brand identifier for a zone's output device.

    Priority order:
    1. capabilities["manufacturer"] — populated by ssdp.py since v0.8.x
    2. capabilities["model"]       — always present for DLNA devices; model
       names are manufacturer-unique in practice (e.g. "Era 100" is Sonos-only)
    3. Empty string                — unknown / local / AirPlay without info
    """
    if not output_device_id or not deps.discovery_manager:
        return ""
    device = deps.discovery_manager.get_device(output_device_id)
    if not device:
        return ""
    caps = device.capabilities
    return caps.get("manufacturer") or caps.get("model") or ""


def _brand_info(zone_instances: list) -> tuple[bool, str]:
    """Compute (auto_synced, group_manufacturer) for a list of ZoneInstance.

    auto_synced is True when every zone has the same non-empty brand key,
    meaning their buffering latency is identical and calibration is not needed.
    """
    keys = [_resolve_zone_brand_key(z.output_device_id) for z in zone_instances]
    non_empty = [k for k in keys if k]
    if non_empty and len(set(non_empty)) == 1 and len(non_empty) == len(zone_instances):
        return True, non_empty[0]
    return False, ""


@router.post("/group", response_model=ZoneGroupResponse)
async def group_zones(request: ZoneGroupRequest):
    """Group zones for synchronized multi-room playback."""
    if not deps.group_manager:
        raise HTTPException(status_code=503, detail="Zone grouping not available")
    leader = deps.zone_manager.get_zone(request.leader_id)
    if not leader:
        raise HTTPException(status_code=404, detail="Leader zone not found")

    followers = []
    for zid in request.zone_ids:
        if zid == request.leader_id:
            continue
        zone = deps.zone_manager.get_zone(zid)
        if zone:
            followers.append(zone)

    if not followers:
        raise HTTPException(status_code=400, detail="No valid follower zones")

    group = await deps.group_manager.create_group(leader, followers)
    auto_synced, group_manufacturer = _brand_info(group.all_zones)
    return ZoneGroupResponse(
        group_id=group.group_id,
        leader_id=leader.zone_id,
        zone_ids=group.zone_ids,
        auto_synced=auto_synced,
        group_manufacturer=group_manufacturer,
    )


@router.delete("/group/{group_id}", status_code=204)
async def ungroup_zones(group_id: str):
    """Dissolve a zone group."""
    if not deps.group_manager:
        raise HTTPException(status_code=503, detail="Zone grouping not available")
    await deps.group_manager.dissolve_group(group_id)


@router.get("/groups/list", response_model=list[ZoneGroupResponse])
async def list_groups():
    """List all active zone groups."""
    if not deps.group_manager:
        raise HTTPException(status_code=503, detail="Zone grouping not available")
    groups = deps.group_manager.list_groups()
    result = []
    for g in groups:
        auto_synced, group_manufacturer = _brand_info(g.all_zones)
        result.append(ZoneGroupResponse(
            group_id=g.group_id,
            leader_id=g.leader.zone_id,
            zone_ids=g.zone_ids,
            auto_synced=auto_synced,
            group_manufacturer=group_manufacturer,
        ))
    return result


@router.get("/{zone_id}/crossfade")
async def get_crossfade(zone_id: int):
    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail=f"Zone {zone_id} not found")
    return {
        "enabled": zone.player._crossfade_enabled,
        "duration": zone.player._crossfade_duration,
    }


@router.post("/{zone_id}/crossfade")
async def set_zone_crossfade(zone_id: int, body: dict = {}):
    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail=f"Zone {zone_id} not found")
    enabled = body.get("enabled", True)
    duration = body.get("duration", 3.0)
    zone.player._crossfade_enabled = enabled
    zone.player._crossfade_duration = duration
    return {"enabled": enabled, "duration": duration}


# =========================================================================
# OpenHome Pins / Presets (Phase 5)
# =========================================================================

def _get_openhome_output(zone_id: int):
    """Return the OpenHomeOutput for a zone, or raise 400/404."""
    from tune_server.outputs.openhome import OpenHomeOutput

    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")
    output = zone.output
    if not isinstance(output, OpenHomeOutput):
        raise HTTPException(status_code=400, detail="Zone is not an OpenHome device")
    return zone, output


@router.get("/{zone_id}/pins")
async def get_zone_pins(zone_id: int):
    """List all pins/presets for an OpenHome device zone."""
    _, output = _get_openhome_output(zone_id)
    if not output.has_pins():
        return {"supported": False, "pins": [], "max_slots": 0}
    try:
        pins = await output.get_pins()
        max_slots = await output.get_device_max_pins()
        return {"supported": True, "pins": pins, "max_slots": max_slots}
    except Exception as exc:
        logger.warning("pins_get_error", zone_id=zone_id, error=str(exc))
        raise HTTPException(status_code=502, detail=f"Failed to read pins: {exc}")


@router.post("/{zone_id}/pins")
async def set_zone_pin(zone_id: int, body: dict = {}):
    """Set a pin on an OpenHome device.

    Body: ``{"index": 0, "title": "...", "uri": "...",
             "mode": "local", "type": "playlist",
             "description": "", "artwork_uri": "", "shuffle": false}``
    """
    _, output = _get_openhome_output(zone_id)
    if not output.has_pins():
        raise HTTPException(status_code=400, detail="Device does not support pins")

    index = body.get("index")
    title = body.get("title", "")
    uri = body.get("uri", "")
    if index is None or not title:
        raise HTTPException(status_code=422, detail="index and title are required")

    try:
        await output.set_pin(
            index=int(index),
            title=title,
            uri=uri,
            mode=body.get("mode", "local"),
            type_=body.get("type", "playlist"),
            description=body.get("description", ""),
            artwork_uri=body.get("artwork_uri", ""),
            shuffle=body.get("shuffle", False),
        )
        return {"status": "ok", "index": index}
    except Exception as exc:
        logger.warning("pins_set_error", zone_id=zone_id, error=str(exc))
        raise HTTPException(status_code=502, detail=f"Failed to set pin: {exc}")


@router.delete("/{zone_id}/pins/{index}")
async def clear_zone_pin(zone_id: int, index: int):
    """Clear a pin slot on an OpenHome device by its slot index.

    Finds the pin ID at the given index and clears it.
    """
    _, output = _get_openhome_output(zone_id)
    if not output.has_pins():
        raise HTTPException(status_code=400, detail="Device does not support pins")

    try:
        pins = await output.get_pins()
        # Find pin at the requested index
        target_pin = None
        for pin in pins:
            if pin.get("index") == index:
                target_pin = pin
                break
        if not target_pin:
            raise HTTPException(status_code=404, detail=f"No pin at index {index}")

        pin_id = target_pin.get("id", 0)
        await output.clear_pin(pin_id)
        return {"status": "ok", "index": index}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("pins_clear_error", zone_id=zone_id, index=index, error=str(exc))
        raise HTTPException(status_code=502, detail=f"Failed to clear pin: {exc}")


@router.post("/{zone_id}/pins/{index}/invoke")
async def invoke_zone_pin(zone_id: int, index: int):
    """Invoke (trigger playback of) a pin on an OpenHome device."""
    _, output = _get_openhome_output(zone_id)
    if not output.has_pins():
        raise HTTPException(status_code=400, detail="Device does not support pins")

    try:
        await output.invoke_pin(index)
        return {"status": "ok", "index": index}
    except Exception as exc:
        logger.warning("pins_invoke_error", zone_id=zone_id, index=index, error=str(exc))
        raise HTTPException(status_code=502, detail=f"Failed to invoke pin: {exc}")


@router.post("/{zone_id}/pins/from-queue")
async def save_queue_as_pin(zone_id: int, body: dict = {}):
    """Save the current queue as a pin/preset on the OpenHome device.

    Body: ``{"title": "My Preset", "index": null}``
    If ``index`` is null, the first available slot is used.
    """
    _, output = _get_openhome_output(zone_id)
    if not output.has_pins():
        raise HTTPException(status_code=400, detail="Device does not support pins")

    title = body.get("title", "Tune Queue")
    index = body.get("index")

    try:
        slot = await output.save_queue_as_pin(title=title, index=index)
        if slot < 0:
            raise HTTPException(status_code=409, detail="Could not save pin (queue empty or no slots)")
        return {"status": "ok", "index": slot, "title": title}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("pins_save_queue_error", zone_id=zone_id, error=str(exc))
        raise HTTPException(status_code=502, detail=f"Failed to save queue as pin: {exc}")

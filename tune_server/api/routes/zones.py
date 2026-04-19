from __future__ import annotations

from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps
from tune_server.models import Zone, ZoneCreateRequest, ZoneUpdateRequest, ZoneGroupRequest, ZoneGroupResponse, StereoPairRequest, StereoPairResponse

router = APIRouter(prefix="/zones", tags=["zones"])


@router.get("", response_model=list[Zone])
async def list_zones():
    """List all configured zones."""
    zones = deps.zone_manager.list_zones()
    return [z.to_model() for z in zones]


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
    """Partial update of a zone (only provided fields are changed)."""
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
    return ZoneGroupResponse(
        group_id=group.group_id,
        leader_id=leader.zone_id,
        zone_ids=group.zone_ids,
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
    return [
        ZoneGroupResponse(
            group_id=g.group_id,
            leader_id=g.leader.zone_id,
            zone_ids=g.zone_ids,
        )
        for g in groups
    ]

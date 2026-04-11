"""Zone Manager API routes — hot-swap, groups, profiles, mute, volume."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tune_server.api.deps import deps
from tune_server.zones.gapless import check_group_gapless
from tune_server.zones.latency import LatencyMeasurer, ZoneHealthMonitor

router = APIRouter(prefix="/zone-manager", tags=["zone-manager"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class HotSwapRequest(BaseModel):
    output_type: str
    output_device_id: str | None = None


class CreateGroupRequest(BaseModel):
    name: str | None = None
    leader_zone_id: int
    zone_ids: list[int]
    master_volume: float = 0.5


class UpdateGroupVolumeRequest(BaseModel):
    master_volume: float | None = None
    offsets: dict[int, float] | None = None  # zone_id -> offset


class MuteZoneRequest(BaseModel):
    muted: bool


class CreateProfileRequest(BaseModel):
    name: str
    description: str | None = None
    icon: str | None = None


# ---------------------------------------------------------------------------
# Hot-swap — change device on a zone without recreating it
# ---------------------------------------------------------------------------

@router.post("/zones/{zone_id}/hot-swap")
async def hot_swap_device(zone_id: int, req: HotSwapRequest):
    """Change the output device of a zone. Stops playback, swaps device."""
    zm = deps.zone_manager
    zone = zm.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    # Stop current playback
    try:
        await zone.player.stop()
    except Exception:
        pass

    # Update zone output
    try:
        await zm.set_output(zone_id, req.output_type, req.output_device_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Update DB
    await deps.db.execute(
        "UPDATE zones SET output_type = ?, output_device_id = ? WHERE id = ?",
        (req.output_type, req.output_device_id, zone_id),
    )
    await deps.db.commit()

    return {"ok": True, "zone_id": zone_id, "new_device": req.output_device_id}


# ---------------------------------------------------------------------------
# Mute / Unmute a zone (within a group)
# ---------------------------------------------------------------------------

@router.post("/zones/{zone_id}/mute")
async def mute_zone(zone_id: int, req: MuteZoneRequest):
    """Mute or unmute a zone. If in a group, stays in the group but silent."""
    await deps.db.execute(
        "UPDATE zones SET muted = ? WHERE id = ?",
        (1 if req.muted else 0, zone_id),
    )
    await deps.db.commit()

    # Actually mute the output
    zm = deps.zone_manager
    zone = zm.get_zone(zone_id)
    if zone:
        try:
            if req.muted:
                await zone.output.set_volume(0.0)
            else:
                # Restore volume
                row = await deps.db.fetchone("SELECT volume FROM zones WHERE id = ?", (zone_id,))
                if row:
                    await zone.output.set_volume(row["volume"])
        except Exception:
            pass

    return {"ok": True, "zone_id": zone_id, "muted": req.muted}


# ---------------------------------------------------------------------------
# Volume — always accessible even without playback
# ---------------------------------------------------------------------------

@router.post("/zones/{zone_id}/volume")
async def set_zone_volume(zone_id: int, volume: float):
    """Set zone volume (0.0-1.0). Works even without active playback."""
    await deps.db.execute(
        "UPDATE zones SET volume = ? WHERE id = ?",
        (max(0.0, min(1.0, volume)), zone_id),
    )
    await deps.db.commit()

    zm = deps.zone_manager
    zone = zm.get_zone(zone_id)
    if zone:
        try:
            await zone.output.set_volume(volume)
        except Exception:
            pass  # Device offline — volume saved for when it comes back

    return {"ok": True, "volume": volume}


# ---------------------------------------------------------------------------
# Groups — persisted multi-room
# ---------------------------------------------------------------------------

@router.get("/groups")
async def list_groups():
    """List all persisted zone groups."""
    rows = await deps.db.fetchall("SELECT * FROM zone_groups ORDER BY created_at")
    groups = []
    for r in rows:
        members = await deps.db.fetchall(
            "SELECT * FROM zone_group_members WHERE group_id = ?", (r["id"],))
        groups.append({
            "id": r["id"],
            "name": r["name"],
            "leader_zone_id": r["leader_zone_id"],
            "master_volume": r["master_volume"],
            "members": [
                {"zone_id": m["zone_id"], "volume_offset": m["volume_offset"], "muted": bool(m["muted"])}
                for m in members
            ],
        })
    return groups


@router.post("/groups")
async def create_group(req: CreateGroupRequest):
    """Create a persisted zone group."""
    group_id = uuid.uuid4().hex[:8]

    await deps.db.execute(
        "INSERT INTO zone_groups (id, name, leader_zone_id, master_volume) VALUES (?, ?, ?, ?)",
        (group_id, req.name, req.leader_zone_id, req.master_volume),
    )

    # Add members
    for zid in req.zone_ids:
        await deps.db.execute(
            "INSERT OR REPLACE INTO zone_group_members (group_id, zone_id) VALUES (?, ?)",
            (group_id, zid),
        )
        # Tag zone with group_id
        await deps.db.execute(
            "UPDATE zones SET group_id = ? WHERE id = ?",
            (group_id, zid),
        )

    await deps.db.commit()

    # Activate group in zone manager
    zm = deps.zone_manager
    try:
        await zm.group_zones(req.leader_zone_id, req.zone_ids)
    except Exception:
        pass

    return {"id": group_id, "name": req.name, "zone_ids": req.zone_ids}


@router.delete("/groups/{group_id}")
async def dissolve_group(group_id: str):
    """Dissolve a zone group."""
    # Clear group_id from zones
    await deps.db.execute("UPDATE zones SET group_id = NULL WHERE group_id = ?", (group_id,))
    await deps.db.execute("DELETE FROM zone_group_members WHERE group_id = ?", (group_id,))
    await deps.db.execute("DELETE FROM zone_groups WHERE id = ?", (group_id,))
    await deps.db.commit()

    # Dissolve in zone manager
    zm = deps.zone_manager
    try:
        await zm.ungroup_zones(group_id)
    except Exception:
        pass

    return {"ok": True}


@router.post("/groups/{group_id}/volume")
async def set_group_volume(group_id: str, req: UpdateGroupVolumeRequest):
    """Set master volume and/or per-zone offsets for a group."""
    if req.master_volume is not None:
        await deps.db.execute(
            "UPDATE zone_groups SET master_volume = ? WHERE id = ?",
            (req.master_volume, group_id),
        )

    if req.offsets:
        for zone_id, offset in req.offsets.items():
            await deps.db.execute(
                "UPDATE zone_group_members SET volume_offset = ? WHERE group_id = ? AND zone_id = ?",
                (offset, group_id, zone_id),
            )

    await deps.db.commit()

    # Apply volumes to outputs
    row = await deps.db.fetchone("SELECT master_volume FROM zone_groups WHERE id = ?", (group_id,))
    master = row["master_volume"] if row else 0.5
    members = await deps.db.fetchall(
        "SELECT zone_id, volume_offset, muted FROM zone_group_members WHERE group_id = ?", (group_id,))

    zm = deps.zone_manager
    for m in members:
        if m["muted"]:
            continue
        effective = max(0.0, min(1.0, master + m["volume_offset"]))
        zone = zm.get_zone(m["zone_id"])
        if zone:
            try:
                await zone.output.set_volume(effective)
            except Exception:
                pass

    return {"ok": True, "master_volume": master}


# ---------------------------------------------------------------------------
# Profiles / Scenarios
# ---------------------------------------------------------------------------

@router.get("/profiles")
async def list_profiles():
    """List saved zone profiles/scenarios."""
    rows = await deps.db.fetchall("SELECT * FROM zone_profiles ORDER BY name")
    return [
        {"id": r["id"], "name": r["name"], "description": r["description"],
         "config": json.loads(r["config"]) if r["config"] else {}, "icon": r["icon"]}
        for r in rows
    ]


@router.post("/profiles")
async def create_profile(req: CreateProfileRequest):
    """Save current zone config as a named profile."""
    # Snapshot current state
    zones = await deps.db.fetchall("SELECT id, name, volume, group_id, muted FROM zones")
    groups = await deps.db.fetchall("SELECT * FROM zone_groups")

    config = {
        "zones": [{"id": z["id"], "volume": z["volume"], "muted": bool(z["muted"])} for z in zones],
        "groups": [
            {"id": g["id"], "name": g["name"], "leader_zone_id": g["leader_zone_id"],
             "master_volume": g["master_volume"]}
            for g in groups
        ],
    }

    await deps.db.execute(
        "INSERT INTO zone_profiles (name, description, config, icon) VALUES (?, ?, ?, ?)",
        (req.name, req.description, json.dumps(config), req.icon),
    )
    await deps.db.commit()
    row = await deps.db.fetchone("SELECT last_insert_rowid() as id")
    return {"id": row["id"], "name": req.name, "config": config}


@router.post("/profiles/{profile_id}/activate")
async def activate_profile(profile_id: int):
    """Activate a saved zone profile — restore groups + volumes."""
    row = await deps.db.fetchone("SELECT * FROM zone_profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")

    config = json.loads(row["config"]) if row["config"] else {}
    zm = deps.zone_manager

    # Restore zone volumes + mute states
    for z in config.get("zones", []):
        await deps.db.execute(
            "UPDATE zones SET volume = ?, muted = ? WHERE id = ?",
            (z.get("volume", 0.5), 1 if z.get("muted") else 0, z["id"]),
        )
        zone = zm.get_zone(z["id"])
        if zone:
            try:
                vol = 0.0 if z.get("muted") else z.get("volume", 0.5)
                await zone.output.set_volume(vol)
            except Exception:
                pass

    # Restore groups
    # First dissolve all current groups
    current_groups = await deps.db.fetchall("SELECT id FROM zone_groups")
    for g in current_groups:
        await deps.db.execute("UPDATE zones SET group_id = NULL WHERE group_id = ?", (g["id"],))
        await deps.db.execute("DELETE FROM zone_group_members WHERE group_id = ?", (g["id"],))
        await deps.db.execute("DELETE FROM zone_groups WHERE id = ?", (g["id"],))

    # Recreate groups from profile
    for g in config.get("groups", []):
        await deps.db.execute(
            "INSERT INTO zone_groups (id, name, leader_zone_id, master_volume) VALUES (?, ?, ?, ?)",
            (g["id"], g.get("name"), g["leader_zone_id"], g.get("master_volume", 0.5)),
        )

    await deps.db.commit()
    return {"ok": True, "profile": row["name"]}


@router.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: int):
    """Delete a zone profile."""
    await deps.db.execute("DELETE FROM zone_profiles WHERE id = ?", (profile_id,))
    await deps.db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Latency measurement & calibration
# ---------------------------------------------------------------------------

@router.post("/zones/{zone_id}/measure-latency")
async def measure_latency(zone_id: int):
    """Measure output latency for a zone. Returns one-way latency in ms."""
    zm = deps.zone_manager
    zone = zm.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    latency = await LatencyMeasurer.measure_zone_latency(zone)
    if latency is None:
        raise HTTPException(status_code=503, detail="Latency measurement failed")

    return {"zone_id": zone_id, "latency_ms": latency}


@router.post("/groups/{group_id}/calibrate")
async def calibrate_group(group_id: str):
    """Auto-calibrate sync delays for all zones in a group."""
    zm = deps.zone_manager

    # Find leader and followers
    group_row = await deps.db.fetchone("SELECT * FROM zone_groups WHERE id = ?", (group_id,))
    if not group_row:
        raise HTTPException(status_code=404, detail="Group not found")

    leader = zm.get_zone(group_row["leader_zone_id"])
    if not leader:
        raise HTTPException(status_code=404, detail="Leader zone not found")

    member_rows = await deps.db.fetchall(
        "SELECT zone_id FROM zone_group_members WHERE group_id = ?", (group_id,))
    followers = [zm.get_zone(m["zone_id"]) for m in member_rows
                 if m["zone_id"] != leader.zone_id and zm.get_zone(m["zone_id"])]

    results = await LatencyMeasurer.auto_calibrate_group(leader, followers, db=deps.db)
    return {"group_id": group_id, "calibration": results}


# ---------------------------------------------------------------------------
# Health monitoring
# ---------------------------------------------------------------------------

@router.get("/zones/{zone_id}/health")
async def zone_health(zone_id: int):
    """Check health of a zone's output device."""
    zm = deps.zone_manager
    zone = zm.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    return await ZoneHealthMonitor.check_zone_health(zone)


@router.get("/groups/{group_id}/health")
async def group_health(group_id: str):
    """Check health of all zones in a group."""
    zm = deps.zone_manager

    group_row = await deps.db.fetchone("SELECT * FROM zone_groups WHERE id = ?", (group_id,))
    if not group_row:
        raise HTTPException(status_code=404, detail="Group not found")

    leader = zm.get_zone(group_row["leader_zone_id"])
    if not leader:
        raise HTTPException(status_code=404, detail="Leader zone not found")

    member_rows = await deps.db.fetchall(
        "SELECT zone_id FROM zone_group_members WHERE group_id = ?", (group_id,))
    followers = [zm.get_zone(m["zone_id"]) for m in member_rows
                 if m["zone_id"] != leader.zone_id and zm.get_zone(m["zone_id"])]

    return await ZoneHealthMonitor.check_group_health(leader, followers)


@router.get("/sync/stats")
async def sync_stats():
    """Get drift statistics from the sync engine."""
    se = deps.sync_engine
    if not se:
        return {"stats": {}}
    return {"stats": se.get_drift_stats()}


# ---------------------------------------------------------------------------
# Gapless multi-room check
# ---------------------------------------------------------------------------

@router.get("/groups/{group_id}/gapless")
async def check_gapless(group_id: str):
    """Check if all zones in a group support gapless playback."""
    zm = deps.zone_manager
    group_row = await deps.db.fetchone("SELECT * FROM zone_groups WHERE id = ?", (group_id,))
    if not group_row:
        raise HTTPException(status_code=404, detail="Group not found")

    member_rows = await deps.db.fetchall(
        "SELECT zone_id FROM zone_group_members WHERE group_id = ?", (group_id,))
    zones = [zm.get_zone(m["zone_id"]) for m in member_rows if zm.get_zone(m["zone_id"])]

    capable = await check_group_gapless(zones)
    return {
        "group_id": group_id,
        "gapless_capable": capable,
        "zones": [
            {"zone_id": z.zone_id, "name": z.name,
             "supports_gapless": getattr(getattr(z.output, 'capabilities', None), 'supports_gapless', False)}
            for z in zones
        ],
    }


# ---------------------------------------------------------------------------
# Zone status overview
# ---------------------------------------------------------------------------

@router.get("/overview")
async def zone_overview():
    """Complete overview: all zones with status, groups, profiles."""
    zm = deps.zone_manager
    all_zones = zm.list_zones() if hasattr(zm, 'list_zones') else []

    zones = []
    for z in all_zones:
        zone_data = z.to_model() if hasattr(z, 'to_model') else {"id": z.zone_id, "name": z.name}
        # Add online/muted from DB
        row = await deps.db.fetchone("SELECT muted, online FROM zones WHERE id = ?", (z.zone_id,))
        if row:
            zone_data["muted"] = bool(row["muted"]) if "muted" in row.keys() else False
            zone_data["online"] = bool(row["online"]) if "online" in row.keys() else True
        zones.append(zone_data)

    groups = await list_groups()
    profiles_rows = await deps.db.fetchall("SELECT id, name, icon FROM zone_profiles ORDER BY name")
    profiles = [{"id": r["id"], "name": r["name"], "icon": r["icon"]} for r in profiles_rows]

    # Sync stats
    se = deps.sync_engine
    drift = se.get_drift_stats() if se else {}

    return {
        "zones": zones,
        "groups": groups,
        "profiles": profiles,
        "sync_stats": drift,
    }

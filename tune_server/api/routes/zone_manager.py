"""Zone Manager API routes — hot-swap, groups, profiles, mute, volume."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tune_server.api.deps import deps

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

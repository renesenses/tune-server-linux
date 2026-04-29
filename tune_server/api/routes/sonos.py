"""REST surface for the Sonos zone type.

Tune zones use OutputType.SONOS when they target a Sonos speaker (or
a Sonos native group). This router exposes:

  GET    /api/v1/sonos/speakers
  POST   /api/v1/sonos/discover                       (force re-scan)
  POST   /api/v1/sonos/groups                         body: {coordinator_uid, member_uids}
  POST   /api/v1/sonos/speakers/{uid}/unjoin

Discovery is best-effort over UDP 1900 (multicast). 503 when SoCo
isn't installed or no speakers found yet.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tune_server.api.deps import deps

router = APIRouter(prefix="/sonos", tags=["sonos"])


class SetGroupRequest(BaseModel):
    coordinator_uid: str
    member_uids: list[str] = []


def _mgr_or_503():
    mgr = getattr(deps, "sonos_manager", None)
    if mgr is None or not mgr.is_supported:
        raise HTTPException(status_code=503, detail="sonos_unavailable")
    return mgr


@router.get("/speakers")
async def list_speakers() -> list[dict]:
    mgr = _mgr_or_503()
    speakers = await mgr.list_speakers()
    return [
        {
            "uid": sp.uid, "name": sp.name, "ip": sp.ip,
            "is_coordinator": sp.is_coordinator, "group_uid": sp.group_uid,
        }
        for sp in speakers
    ]


@router.post("/discover")
async def discover() -> list[dict]:
    mgr = _mgr_or_503()
    speakers = await mgr.discover()
    return [
        {
            "uid": sp.uid, "name": sp.name, "ip": sp.ip,
            "is_coordinator": sp.is_coordinator, "group_uid": sp.group_uid,
        }
        for sp in speakers
    ]


@router.post("/groups")
async def set_group(body: SetGroupRequest) -> dict:
    """Form a native Sonos group : coordinator + members. Member
    speakers leave whatever group they were in."""
    mgr = _mgr_or_503()
    await mgr.set_group(body.coordinator_uid, body.member_uids)
    return {
        "coordinator_uid": body.coordinator_uid,
        "member_uids": body.member_uids,
    }


@router.post("/speakers/{uid}/unjoin")
async def unjoin_speaker(uid: str) -> dict:
    mgr = _mgr_or_503()
    await mgr.unjoin(uid)
    return {"uid": uid, "unjoined": True}

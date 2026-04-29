"""REST surface for the Snapcast zone type.

Tune zones use OutputType.SNAPCAST when they target snapcast clients
(PCs/phones/RPi running snapclient). This router exposes:

  GET    /api/v1/snapcast/clients
  POST   /api/v1/snapcast/clients/{client_id}/assign  body: {zone_id}
  DELETE /api/v1/snapcast/clients/{client_id}/assign
  GET    /api/v1/snapcast/status

Skeleton — endpoints return placeholders until SnapcastManager runtime
lands (v0.8.0 milestone task #45).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tune_server.api.deps import deps

router = APIRouter(prefix="/snapcast", tags=["snapcast"])


class AssignClientRequest(BaseModel):
    zone_id: int


@router.get("/status")
async def snapcast_status() -> dict:
    """Health probe — returns whether the embedded snapserver is reachable."""
    mgr = getattr(deps, "snapcast_manager", None)
    if mgr is None:
        return {"enabled": False, "reason": "no_manager"}
    if not mgr.is_supported:
        return {"enabled": False, "reason": "unsupported_platform"}
    if mgr.binary_path is None:
        return {"enabled": False, "reason": "snapserver_not_installed"}
    return {
        "enabled": True,
        "binary": str(mgr.binary_path),
        "stream_count": len(getattr(mgr, "_streams", {})),
    }


@router.get("/clients")
async def list_clients() -> list[dict]:
    mgr = getattr(deps, "snapcast_manager", None)
    if mgr is None or not mgr.is_supported:
        raise HTTPException(status_code=503, detail="snapcast_unavailable")
    clients = await mgr.list_clients()
    return [
        {
            "id": c.id, "name": c.name, "host": c.host, "mac": c.mac,
            "connected": c.connected, "volume": c.volume,
        }
        for c in clients
    ]


@router.post("/clients/{client_id}/assign")
async def assign_client(client_id: str, body: AssignClientRequest) -> dict:
    """Bind a snapclient UUID to a Tune zone. Multi-bind is allowed
    (e.g. living-room left + right speakers as one zone)."""
    mgr = getattr(deps, "snapcast_manager", None)
    if mgr is None or not mgr.is_supported:
        raise HTTPException(status_code=503, detail="snapcast_unavailable")
    # TODO #45: persist to Zone.snapcast_client_ids + JSON-RPC Group.SetClients.
    raise HTTPException(status_code=501, detail="snapcast_assign_not_implemented")


@router.delete("/clients/{client_id}/assign")
async def unassign_client(client_id: str) -> dict:
    mgr = getattr(deps, "snapcast_manager", None)
    if mgr is None or not mgr.is_supported:
        raise HTTPException(status_code=503, detail="snapcast_unavailable")
    # TODO #45: remove from Zone.snapcast_client_ids + JSON-RPC.
    raise HTTPException(status_code=501, detail="snapcast_unassign_not_implemented")

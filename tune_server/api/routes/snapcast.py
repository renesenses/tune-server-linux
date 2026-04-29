"""REST surface for the Snapcast zone type.

Tune zones use OutputType.SNAPCAST when they target snapcast clients
(PCs/phones/RPi running snapclient). This router exposes:

  GET    /api/v1/snapcast/clients
  POST   /api/v1/snapcast/clients/{client_id}/assign  body: {zone_id}
  DELETE /api/v1/snapcast/clients/{client_id}/assign  body: {zone_id}
  GET    /api/v1/snapcast/status
"""
from __future__ import annotations

import json

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


def _parse_client_ids(raw: str | None) -> list[str]:
    """`Zone.snapcast_client_ids` is stored as a JSON-encoded text
    column (works on both SQLite and PostgreSQL without adding the
    pg-only ARRAY type). Parse defensively — legacy zones may have
    NULL or empty strings."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(x) for x in parsed if x]


@router.post("/clients/{client_id}/assign")
async def assign_client(client_id: str, body: AssignClientRequest) -> dict:
    """Bind a snapclient UUID to a Tune zone. Multi-bind is allowed
    (e.g. living-room left + right speakers as one zone). Persists the
    UUID into `Zone.snapcast_client_ids` and routes the snapclient
    onto the zone's snapcast group via JSON-RPC."""
    mgr = getattr(deps, "snapcast_manager", None)
    if mgr is None or not mgr.is_supported:
        raise HTTPException(status_code=503, detail="snapcast_unavailable")
    if deps.zone_repo is None:
        raise HTTPException(status_code=503, detail="zone_repo_unavailable")

    zone = await deps.zone_repo.get(body.zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail=f"zone_{body.zone_id}_not_found")
    if zone.get("output_type") != "snapcast":
        raise HTTPException(
            status_code=400,
            detail=f"zone_{body.zone_id}_not_snapcast (output_type={zone.get('output_type')})",
        )

    current_ids = _parse_client_ids(zone.get("snapcast_client_ids"))
    if client_id not in current_ids:
        current_ids.append(client_id)
    stream_name = zone.get("snapcast_stream_name") or f"tune-zone-{body.zone_id}"

    await deps.zone_repo.update(
        body.zone_id,
        snapcast_client_ids=json.dumps(current_ids),
        snapcast_stream_name=stream_name,
    )
    await mgr.set_clients_for_stream(stream_name, current_ids)
    return {"zone_id": body.zone_id, "stream_name": stream_name, "client_ids": current_ids}


@router.delete("/clients/{client_id}/assign")
async def unassign_client(client_id: str, body: AssignClientRequest) -> dict:
    """Remove a snapclient UUID from a zone. The client falls back to
    snapcast's "default" group (silent stream) until reassigned."""
    mgr = getattr(deps, "snapcast_manager", None)
    if mgr is None or not mgr.is_supported:
        raise HTTPException(status_code=503, detail="snapcast_unavailable")
    if deps.zone_repo is None:
        raise HTTPException(status_code=503, detail="zone_repo_unavailable")

    zone = await deps.zone_repo.get(body.zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail=f"zone_{body.zone_id}_not_found")

    current_ids = _parse_client_ids(zone.get("snapcast_client_ids"))
    new_ids = [cid for cid in current_ids if cid != client_id]
    stream_name = zone.get("snapcast_stream_name") or f"tune-zone-{body.zone_id}"

    await deps.zone_repo.update(
        body.zone_id,
        snapcast_client_ids=json.dumps(new_ids),
    )
    # Empty client list = leave the snapcast group empty. The unassigned
    # client will reattach to whatever stream it was previously on (or
    # snapcast picks a default).
    await mgr.set_clients_for_stream(stream_name, new_ids)
    return {"zone_id": body.zone_id, "stream_name": stream_name, "client_ids": new_ids}

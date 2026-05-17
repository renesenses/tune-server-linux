"""API routes for Tune peer discovery — browse and interact with other
Tune servers discovered on the LAN via mDNS.
"""

from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps

logger = structlog.get_logger()

router = APIRouter(prefix="/system/peers", tags=["peers"])


@router.get("")
async def list_peers():
    """Return all Tune servers currently visible on the local network."""
    dm = deps.discovery_manager
    if not dm or not dm.tune:
        return []
    return [peer.to_dict() for peer in dm.tune.peers.values()]


@router.post("/{ip}/browse")
async def browse_peer(ip: str, port: int = 8888):
    """Proxy a library browse request to another Tune server.

    Returns the remote server's albums, similar to ``GET /library/albums``.
    """
    dm = deps.discovery_manager
    if not dm or not dm.tune:
        raise HTTPException(status_code=503, detail="Peer discovery not available")

    # Verify the peer is actually discovered
    peer_id = f"{ip}:{port}"
    peer = dm.tune.peers.get(peer_id)
    if not peer:
        raise HTTPException(status_code=404, detail=f"Peer {peer_id} not found")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"http://{ip}:{port}/api/v1/library/albums")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        logger.warning("peer_browse_failed", peer=peer_id, error=str(exc))
        raise HTTPException(status_code=502, detail=f"Cannot reach peer: {exc}")


@router.post("/{ip}/transfer")
async def transfer_to_peer(ip: str, port: int = 8888, zone_id: int = 1):
    """Transfer current playback state to another Tune server.

    Sends the current queue and position to the remote server so it can
    resume playback seamlessly.
    """
    dm = deps.discovery_manager
    if not dm or not dm.tune:
        raise HTTPException(status_code=503, detail="Peer discovery not available")

    peer_id = f"{ip}:{port}"
    peer = dm.tune.peers.get(peer_id)
    if not peer:
        raise HTTPException(status_code=404, detail=f"Peer {peer_id} not found")

    # Get current playback state from local zone
    zm = deps.zone_manager
    if not zm:
        raise HTTPException(status_code=503, detail="Zone manager not available")

    zone = zm.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail=f"Zone {zone_id} not found")

    state = zone.playback_state()
    if not state or not state.get("track"):
        raise HTTPException(status_code=400, detail="Nothing is playing in this zone")

    transfer_payload = {
        "track": state.get("track"),
        "position": state.get("position", 0),
        "queue": state.get("queue", []),
        "source_server": dm.tune._local_ip,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"http://{ip}:{port}/api/v1/playback/zones/{zone_id}/play",
                json=transfer_payload,
            )
            resp.raise_for_status()
            result = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("peer_transfer_failed", peer=peer_id, error=str(exc))
        raise HTTPException(status_code=502, detail=f"Transfer failed: {exc}")

    logger.info("playback_transferred", from_zone=zone_id, to_peer=peer_id)
    return {"status": "transferred", "peer": peer.to_dict(), "remote_response": result}

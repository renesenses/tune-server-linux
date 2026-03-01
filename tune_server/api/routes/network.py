from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Query

from tune_server.api.deps import deps
from tune_server.discovery.network_shares import NetworkShareDiscovery
from tune_server.models import (
    DiscoveredMediaServer,
    MediaServerBrowseResult,
    MountInfo,
    MountRequest,
    NetworkShare,
    ShareProtocol,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/network", tags=["network"])


# --- Network shares ---


@router.get("/shares", response_model=list[NetworkShare])
async def list_network_shares():
    """List discovered SMB/NFS shares on the local network."""
    dm = deps.discovery_manager
    if not dm or not dm.network_shares:
        return []
    return list(dm.network_shares.shares.values())


@router.get("/shares/{share_id:path}/list")
async def list_host_shares(share_id: str):
    """Enumerate available shares/exports on a discovered host."""
    dm = deps.discovery_manager
    if not dm or not dm.network_shares:
        raise HTTPException(status_code=503, detail="Network share discovery not enabled")

    shares = await dm.network_shares.enumerate_host_shares(share_id)
    return {"share_id": share_id, "shares": shares}


@router.get("/scan-host")
async def scan_host(
    host: str = Query(..., description="IP or hostname to scan"),
    protocol: ShareProtocol = Query(ShareProtocol.SMB),
):
    """Scan a specific host for available shares/exports (no mDNS discovery needed)."""
    if protocol == ShareProtocol.SMB:
        shares = await NetworkShareDiscovery._list_smb_shares(host)
    else:
        shares = await NetworkShareDiscovery._list_nfs_exports(host)
    return {"host": host, "protocol": protocol.value, "shares": shares}


# --- DLNA Media Servers ---


@router.get("/media-servers", response_model=list[DiscoveredMediaServer])
async def list_media_servers():
    """List discovered DLNA MediaServer devices."""
    dm = deps.discovery_manager
    if not dm or not dm.media_servers:
        return []
    return list(dm.media_servers.servers.values())


@router.get("/media-servers/{server_id:path}/browse", response_model=MediaServerBrowseResult)
async def browse_media_server(
    server_id: str,
    object_id: str = Query("0", description="ContentDirectory object ID"),
    start: int = Query(0, ge=0),
    count: int = Query(100, ge=1, le=500),
):
    """Browse a DLNA MediaServer's ContentDirectory."""
    dm = deps.discovery_manager
    if not dm or not dm.media_servers:
        raise HTTPException(status_code=503, detail="Media server discovery not enabled")

    upnp_device = dm.media_servers.get_upnp_device(server_id)
    if not upnp_device:
        raise HTTPException(status_code=404, detail="Media server not found")

    from tune_server.network.media_server_browser import MediaServerBrowser

    browser = MediaServerBrowser(upnp_device)
    try:
        return await browser.browse(object_id=object_id, start=start, count=count)
    except Exception as e:
        logger.exception("media_server_browse_error", server_id=server_id, object_id=object_id)
        raise HTTPException(status_code=502, detail=f"Browse failed: {e}")


@router.get("/media-servers/{server_id:path}/item/{item_id}/stream-url")
async def get_media_server_stream_url(server_id: str, item_id: str):
    """Get the stream URL for a specific item on a DLNA MediaServer."""
    dm = deps.discovery_manager
    if not dm or not dm.media_servers:
        raise HTTPException(status_code=503, detail="Media server discovery not enabled")

    upnp_device = dm.media_servers.get_upnp_device(server_id)
    if not upnp_device:
        raise HTTPException(status_code=404, detail="Media server not found")

    from tune_server.network.media_server_browser import MediaServerBrowser

    browser = MediaServerBrowser(upnp_device)
    try:
        # Browse the specific item to get its metadata
        result = await browser.browse(object_id=item_id, start=0, count=1)
        # If browsing the item as parent returns nothing, try browsing its parent
        # and finding it there — but the direct call should work for items
    except Exception:
        pass

    # Alternative: browse parent and find item
    # For now, browse the item's parent with a BrowseMetadata approach
    # We re-browse with the item_id as the direct object
    try:
        cd = browser._find_content_directory_service()
        browse_action = cd.action("Browse")
        raw = await browse_action.async_call(
            ObjectID=item_id,
            BrowseFlag="BrowseMetadata",
            Filter="*",
            StartingIndex=0,
            RequestedCount=1,
            SortCriteria="",
        )
        didl_result = browser._parse_didl(
            raw.get("Result", ""),
            object_id=item_id,
            total=1,
            returned=1,
        )
        if didl_result.items and didl_result.items[0].res_url:
            return {"stream_url": didl_result.items[0].res_url}
    except Exception as e:
        logger.exception("media_server_stream_url_error", server_id=server_id, item_id=item_id)
        raise HTTPException(status_code=502, detail=f"Failed to get stream URL: {e}")

    raise HTTPException(status_code=404, detail="No stream URL found for this item")


# --- Mount management ---


@router.get("/mounts", response_model=list[MountInfo])
async def list_mounts():
    """List all configured network mounts."""
    if not deps.mount_manager:
        return []
    return await deps.mount_manager.list_mounts()


@router.post("/mounts", response_model=MountInfo)
async def create_mount(request: MountRequest):
    """Mount a network share."""
    if not deps.mount_manager:
        raise HTTPException(status_code=503, detail="Mount manager not available")
    return await deps.mount_manager.mount_share(request)


@router.delete("/mounts/{mount_id}", status_code=204)
async def delete_mount(mount_id: int):
    """Unmount and remove a network share."""
    if not deps.mount_manager:
        raise HTTPException(status_code=503, detail="Mount manager not available")
    await deps.mount_manager.unmount_share(mount_id)


@router.post("/mounts/{mount_id}/remount", response_model=MountInfo)
async def remount(mount_id: int):
    """Re-mount an existing network share."""
    if not deps.mount_manager:
        raise HTTPException(status_code=503, detail="Mount manager not available")
    try:
        return await deps.mount_manager.remount(mount_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

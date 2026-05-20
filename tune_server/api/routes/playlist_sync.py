"""Playlist Sync API routes — cross-service playlist orchestrator.

Import, export, and transfer playlists between streaming services
(Tidal, Qobuz, Spotify, Deezer, YouTube Music) and the local library.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from tune_server.api.deps import deps
from tune_server.playlist_sync.orchestrator import (
    NotFoundError,
    PlaylistOrchestrator,
    ServiceError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/playlist-sync", tags=["playlist-sync"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ServiceInfo(BaseModel):
    service: str
    authenticated: bool
    supports_write: bool


class ImportRequest(BaseModel):
    source_service: str
    source_playlist_id: str
    name: Optional[str] = None


class ImportResponse(BaseModel):
    playlist_id: int
    name: str
    tracks_imported: int


class ExportRequest(BaseModel):
    playlist_id: int
    target_service: str
    name: Optional[str] = None


class ExportResponse(BaseModel):
    service_playlist_id: Optional[str] = None
    name: str
    total_tracks: int = 0
    matched: int = 0
    approximate: int = 0
    not_found: int = 0
    tracks: list[dict] = []


class TransferRequest(BaseModel):
    source_service: str
    source_playlist_id: str
    target_service: str
    name: Optional[str] = None


class TransferResponse(BaseModel):
    transfer_id: int
    status: str
    playlist_name: str
    source_service: str
    target_service: str


class TransferStatusResponse(BaseModel):
    transfer_id: int
    status: str
    playlist_name: str
    source_service: str
    target_service: str
    total_tracks: int = 0
    processed: int = 0
    matched: int = 0
    approximate: int = 0
    not_found: int = 0
    error: str = ""
    tracks: list[dict] = []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/services", response_model=list[ServiceInfo])
async def list_services():
    """List connected streaming services that support playlists."""
    orch = PlaylistOrchestrator(deps)
    return orch.list_connected_services()


@router.post("/import", response_model=ImportResponse)
async def import_playlist(body: ImportRequest):
    """Import a playlist from a streaming service into the local library."""
    orch = PlaylistOrchestrator(deps)
    try:
        result = await orch.import_playlist(
            source_service=body.source_service,
            source_playlist_id=body.source_playlist_id,
            name=body.name,
        )
    except ServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return ImportResponse(**result)


@router.post("/export", response_model=ExportResponse)
async def export_playlist(body: ExportRequest):
    """Export a local playlist to a streaming service."""
    orch = PlaylistOrchestrator(deps)
    try:
        result = await orch.export_playlist(
            playlist_id=body.playlist_id,
            target_service=body.target_service,
            name=body.name,
        )
    except ServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return ExportResponse(**result)


@router.post("/transfer", response_model=TransferResponse, deprecated=True)
async def transfer_playlist(body: TransferRequest, response: Response):
    """Transfer a playlist between two streaming services.

    .. deprecated::
        Use POST /api/v1/playlist-manager/transfer instead. This endpoint
        will be removed after 2026-09-01.

    The transfer runs in the background. Use the returned transfer_id
    to poll for progress via GET /playlist-sync/transfer/{id}/status.
    """
    logger.warning(
        "Deprecated endpoint called: POST /api/v1/playlist-sync/transfer — "
        "use POST /api/v1/playlist-manager/transfer instead"
    )
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "2026-09-01"
    response.headers["Link"] = '</api/v1/playlist-manager/transfer>; rel="successor-version"'

    orch = PlaylistOrchestrator(deps)
    try:
        progress = await orch.transfer_playlist(
            source_service=body.source_service,
            source_playlist_id=body.source_playlist_id,
            target_service=body.target_service,
            name=body.name,
        )
    except ServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return TransferResponse(
        transfer_id=progress.id,
        status=progress.status,
        playlist_name=progress.playlist_name,
        source_service=progress.source_service,
        target_service=progress.target_service,
    )


@router.get("/transfer/{transfer_id}/status", response_model=TransferStatusResponse)
async def transfer_status(transfer_id: int):
    """Check the progress of a transfer operation."""
    progress = PlaylistOrchestrator.get_transfer_status(transfer_id)
    if not progress:
        raise HTTPException(status_code=404, detail="Transfer not found")
    return TransferStatusResponse(
        transfer_id=progress.id,
        status=progress.status,
        playlist_name=progress.playlist_name,
        source_service=progress.source_service,
        target_service=progress.target_service,
        total_tracks=progress.total_tracks,
        processed=progress.processed,
        matched=progress.matched,
        approximate=progress.approximate,
        not_found=progress.not_found,
        error=progress.error,
        tracks=progress.track_results,
    )

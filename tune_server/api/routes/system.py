from __future__ import annotations

import asyncio

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from tune_server.api.deps import deps
from tune_server.config import settings
from tune_server.models import ScanStatusResponse, SystemConfigResponse, SystemHealthResponse, SystemStatsResponse

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health", response_model=SystemHealthResponse)
async def health():
    # Core components — affect overall health status
    core_components = {
        "database": deps.db is not None,
        "scanner": deps.scanner is not None,
        "zones": deps.zone_manager is not None,
        "discovery": deps.discovery_manager is not None,
    }
    # Streaming services are optional — report status but don't degrade health
    components = dict(core_components)
    for name, service in list(deps.streaming_services.items()):
        components[name] = service.is_authenticated

    all_core_ok = all(core_components.values())
    return SystemHealthResponse(
        status="ok" if all_core_ok else "degraded",
        components=components,
    )


@router.get("/config", response_model=SystemConfigResponse)
async def get_config():
    return SystemConfigResponse(
        music_dirs=settings.music_dirs,
        api_port=settings.api_port,
        stream_port=settings.stream_port,
        tidal_enabled=settings.tidal_enabled,
        qobuz_enabled=settings.qobuz_enabled,
        youtube_enabled=settings.youtube_enabled,
        amazon_music_enabled=settings.amazon_music_enabled,
        discovery_enabled=settings.discovery_enabled,
    )


@router.post("/scan", status_code=202)
async def trigger_scan(path: Optional[str] = Query(None, description="Scan a single directory instead of all music_dirs")):
    if not deps.scanner:
        raise HTTPException(status_code=503, detail="Scanner not available")

    if deps.scanner.is_scanning:
        raise HTTPException(status_code=409, detail="Scan already in progress")

    if path:
        resolved = str(Path(path).resolve())
        # Verify the path is a configured music_dir
        resolved_dirs = [str(Path(d).resolve()) for d in settings.music_dirs]
        if resolved not in resolved_dirs:
            raise HTTPException(status_code=400, detail="Path is not a configured music directory")
        scan_dirs = [resolved]
    else:
        scan_dirs = settings.music_dirs

    # Run scan in background
    task = asyncio.create_task(deps.scanner.scan(scan_dirs))
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    return {"status": "scan_started", "music_dirs": scan_dirs}


@router.get("/scan/status", response_model=ScanStatusResponse)
async def scan_status():
    return ScanStatusResponse(scanning=deps.scanner.is_scanning if deps.scanner else False)


@router.get("/stats", response_model=SystemStatsResponse)
async def system_stats():
    zones = deps.zone_manager.list_zones() if deps.zone_manager else []
    devices = deps.discovery_manager.list_devices() if deps.discovery_manager else []

    return SystemStatsResponse(
        tracks=await deps.track_repo.count() if deps.track_repo else 0,
        albums=await deps.album_repo.count() if deps.album_repo else 0,
        artists=await deps.artist_repo.count() if deps.artist_repo else 0,
        zones=len(zones),
        devices=len(devices),
    )

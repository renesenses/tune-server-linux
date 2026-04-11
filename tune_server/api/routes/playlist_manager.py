"""Playlist Manager API routes — transfer, sync, backup, merge, export."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from fastapi.responses import PlainTextResponse

from tune_server.api.deps import deps
from tune_server.playlist_manager.models import (
    TransferRequest, TransferResponse,
    BatchTransferRequest, BatchTransferResponse,
    BackupRequest, BackupResponse,
    CreateLinkRequest, PlaylistLink,
    MergeRequest,
    ExportRequest,
    TransferHistoryEntry,
)
from tune_server.playlist_manager.transfer import execute_transfer
from tune_server.playlist_manager.export_import import export_playlist, import_playlist
from tune_server.playlist_manager.sync import sync_playlist
from tune_server.playlist_manager.batch import batch_transfer, merge_playlists

router = APIRouter(prefix="/playlist-manager", tags=["playlist-manager"])


@router.get("/services")
async def get_services_capabilities():
    """List streaming services with their playlist write capabilities."""
    sm = deps.streaming_manager
    services = {}
    for name, status in sm.status.items():
        svc = sm.service(name)
        services[name] = {
            "authenticated": status.get("authenticated", False) if isinstance(status, dict) else getattr(status, "authenticated", False),
            "supports_write": svc.supports_playlist_write if svc else False,
        }
    services["local"] = {"authenticated": True, "supports_write": True}
    return services


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------

@router.post("/transfer", response_model=TransferResponse)
async def transfer_playlist(req: TransferRequest):
    """Transfer a playlist from one service to another with track matching."""
    sm = deps.streaming_manager

    # Get source playlist tracks
    source_svc = sm.service(req.source_service) if req.source_service != "local" else None

    if req.source_service == "local":
        # Local playlist
        tracks_rows = await deps.db.fetchall(
            """SELECT t.title, t.artist_name, a.title as album_title,
                      t.duration_ms, t.source_id, t.isrc
               FROM playlist_tracks pt
               JOIN tracks t ON t.id = pt.track_id
               LEFT JOIN albums a ON a.id = t.album_id
               WHERE pt.playlist_id = ?
               ORDER BY pt.position""",
            (int(req.source_playlist_id),),
        )
        source_tracks = [dict(r) for r in tracks_rows]
        source_name = "Local Playlist"
        row = await deps.db.fetchone(
            "SELECT name FROM playlists WHERE id = ?", (int(req.source_playlist_id),))
        if row:
            source_name = row["name"]
    elif source_svc:
        try:
            playlist_tracks = await source_svc.getPlaylistTracks(req.source_playlist_id)
            source_tracks = [
                {
                    "title": t.get("title", ""),
                    "artist_name": t.get("artist_name", ""),
                    "album_title": t.get("album_title", ""),
                    "duration_ms": t.get("duration_ms", 0),
                    "source_id": t.get("source_id", ""),
                    "isrc": t.get("isrc", ""),
                }
                for t in playlist_tracks
            ]
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to load source playlist: {e}")
        # Get playlist name
        try:
            playlists = await source_svc.getUserPlaylists()
            source_name = next(
                (p.name for p in playlists if p.source_id == req.source_playlist_id),
                req.source_playlist_id,
            )
        except Exception:
            source_name = req.source_playlist_id
    else:
        raise HTTPException(status_code=400, detail=f"Source service '{req.source_service}' not found")

    # Build search function for target service
    if req.target_service == "local":
        # Search local library
        async def search_func(query: str, limit: int):
            rows = await deps.db.fetchall(
                """SELECT t.id as source_id, t.title, t.artist_name,
                          a.title as album_title, t.duration_ms, t.isrc
                   FROM tracks t LEFT JOIN albums a ON a.id = t.album_id
                   WHERE t.title LIKE ? OR t.artist_name LIKE ?
                   LIMIT ?""",
                (f"%{query}%", f"%{query}%", limit),
            )
            return [dict(r) for r in rows]
    else:
        target_svc = sm.service(req.target_service)
        if not target_svc:
            raise HTTPException(status_code=400, detail=f"Target service '{req.target_service}' not found")

        async def search_func(query: str, limit: int):
            results = await target_svc.search(query, limit=limit)
            return [
                {
                    "title": r.title,
                    "artist_name": r.artist,
                    "album_title": r.album or "",
                    "source_id": r.id,
                    "duration_ms": getattr(r, "duration_ms", 0),
                    "isrc": getattr(r, "isrc", ""),
                }
                for r in results
            ]

    # Execute transfer
    result = await execute_transfer(
        source_tracks=source_tracks,
        source_service=req.source_service,
        source_playlist_name=source_name,
        source_playlist_id=req.source_playlist_id,
        target_service=req.target_service,
        target_name=req.target_name,
        search_func=search_func,
        db=deps.db,
        event_bus=deps.event_bus,
        match_threshold=req.match_threshold,
        include_approximate=req.include_approximate,
        dry_run=req.dry_run,
        create_on_target=req.create_on_target,
    )

    return result


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@router.get("/history")
async def get_transfer_history(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    operation: str | None = Query(None),
):
    """List transfer history."""
    sql = "SELECT * FROM transfer_history"
    params: list = []
    if operation:
        sql += " WHERE operation = ?"
        params.append(operation)
    sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = await deps.db.fetchall(sql, tuple(params))
    return [
        TransferHistoryEntry(
            id=r["id"],
            operation=r["operation"],
            source_service=r["source_service"],
            source_playlist_name=r["source_playlist_name"],
            target_service=r["target_service"],
            target_playlist_name=r["target_playlist_name"],
            total_tracks=r["total_tracks"],
            matched=r["matched"],
            approximate=r["approximate"],
            not_found=r["not_found"],
            status=r["status"],
            started_at=r["started_at"],
            completed_at=r["completed_at"],
        )
        for r in rows
    ]


@router.get("/history/{transfer_id}")
async def get_transfer_detail(transfer_id: int):
    """Get detailed transfer results including per-track matches."""
    row = await deps.db.fetchone(
        "SELECT * FROM transfer_history WHERE id = ?", (transfer_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Transfer not found")

    details = json.loads(row["details"]) if row["details"] else []
    return {
        "id": row["id"],
        "operation": row["operation"],
        "source_service": row["source_service"],
        "source_playlist_name": row["source_playlist_name"],
        "target_service": row["target_service"],
        "target_playlist_name": row["target_playlist_name"],
        "total_tracks": row["total_tracks"],
        "matched": row["matched"],
        "approximate": row["approximate"],
        "not_found": row["not_found"],
        "status": row["status"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "tracks": details,
    }


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

@router.post("/backup", response_model=BackupResponse)
async def backup_playlists(req: BackupRequest):
    """Snapshot all playlists from specified services."""
    sm = deps.streaming_manager
    services_to_backup = req.services or list(sm.status.keys()) + ["local"]
    total_playlists = 0
    total_tracks = 0
    service_counts: dict[str, int] = {}

    for svc_name in services_to_backup:
        playlists = []

        if svc_name == "local":
            rows = await deps.db.fetchall("SELECT * FROM playlists ORDER BY name")
            for r in rows:
                tracks_data = []
                if req.include_tracks:
                    track_rows = await deps.db.fetchall(
                        """SELECT t.title, t.artist_name, a.title as album_title,
                                  t.duration_ms, t.source_id, t.isrc
                           FROM playlist_tracks pt
                           JOIN tracks t ON t.id = pt.track_id
                           LEFT JOIN albums a ON a.id = t.album_id
                           WHERE pt.playlist_id = ?
                           ORDER BY pt.position""",
                        (r["id"],),
                    )
                    tracks_data = [dict(tr) for tr in track_rows]

                await deps.db.execute(
                    """INSERT INTO playlist_snapshots
                       (source_service, source_playlist_id, playlist_name, track_count, snapshot_data)
                       VALUES (?, ?, ?, ?, ?)""",
                    ("local", str(r["id"]), r["name"], len(tracks_data), json.dumps(tracks_data)),
                )
                total_playlists += 1
                total_tracks += len(tracks_data)
        else:
            svc = sm.service(svc_name)
            if not svc:
                continue
            try:
                playlists = await svc.getUserPlaylists()
            except Exception:
                continue

            for pl in playlists:
                tracks_data = []
                if req.include_tracks:
                    try:
                        tracks = await svc.getPlaylistTracks(pl.source_id)
                        tracks_data = [
                            {"title": t.get("title", ""), "artist_name": t.get("artist_name", ""),
                             "album_title": t.get("album_title", ""), "duration_ms": t.get("duration_ms", 0),
                             "source_id": t.get("source_id", ""), "isrc": t.get("isrc", "")}
                            for t in tracks
                        ]
                    except Exception:
                        pass

                await deps.db.execute(
                    """INSERT INTO playlist_snapshots
                       (source_service, source_playlist_id, playlist_name, track_count, snapshot_data)
                       VALUES (?, ?, ?, ?, ?)""",
                    (svc_name, pl.source_id, pl.name, len(tracks_data), json.dumps(tracks_data)),
                )
                total_playlists += 1
                total_tracks += len(tracks_data)

        service_counts[svc_name] = total_playlists - sum(
            v for k, v in service_counts.items() if k != svc_name
        )

    await deps.db.commit()

    return BackupResponse(
        backup_id=0,
        playlists_backed_up=total_playlists,
        total_tracks_snapshot=total_tracks,
        services=service_counts,
    )


# ---------------------------------------------------------------------------
# Sync Links
# ---------------------------------------------------------------------------

@router.get("/links")
async def list_links():
    """List all playlist sync links."""
    rows = await deps.db.fetchall("SELECT * FROM playlist_links ORDER BY created_at DESC")
    return [
        PlaylistLink(
            id=r["id"],
            local_playlist_id=r["local_playlist_id"],
            service=r["service"],
            service_playlist_id=r["service_playlist_id"],
            service_playlist_name=r["service_playlist_name"],
            sync_direction=r["sync_direction"],
            last_synced_at=r["last_synced_at"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.post("/links", response_model=PlaylistLink)
async def create_link(req: CreateLinkRequest):
    """Create a sync link between a local playlist and a remote playlist."""
    await deps.db.execute(
        """INSERT INTO playlist_links
           (local_playlist_id, service, service_playlist_id, sync_direction)
           VALUES (?, ?, ?, ?)""",
        (req.local_playlist_id, req.service, req.service_playlist_id, req.sync_direction),
    )
    await deps.db.commit()
    row = await deps.db.fetchone("SELECT * FROM playlist_links WHERE id = last_insert_rowid()")
    return PlaylistLink(
        id=row["id"],
        local_playlist_id=row["local_playlist_id"],
        service=row["service"],
        service_playlist_id=row["service_playlist_id"],
        sync_direction=row["sync_direction"],
        created_at=row["created_at"],
    )


@router.post("/links/{link_id}/sync")
async def trigger_sync(link_id: int):
    """Trigger a sync for a playlist link."""
    row = await deps.db.fetchone("SELECT * FROM playlist_links WHERE id = ?", (link_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Link not found")

    link = dict(row)
    sm = deps.streaming_manager
    svc = sm.service(link["service"])
    if not svc:
        raise HTTPException(status_code=400, detail=f"Service '{link['service']}' not available")

    # Build search func for push matching
    async def search_func(query: str, limit: int):
        results = await svc.search(query, limit=limit)
        return [
            {"title": r.title, "artist_name": r.artist, "source_id": r.id,
             "duration_ms": getattr(r, "duration_ms", 0)}
            for r in results
        ]

    result = await sync_playlist(
        link=link,
        db=deps.db,
        streaming_service=svc,
        search_func=search_func,
    )
    return result


@router.delete("/links/{link_id}")
async def delete_link(link_id: int):
    """Delete a sync link."""
    await deps.db.execute("DELETE FROM playlist_links WHERE id = ?", (link_id,))
    await deps.db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Batch Transfer
# ---------------------------------------------------------------------------

@router.post("/batch-transfer", response_model=BatchTransferResponse)
async def batch_transfer_endpoint(req: BatchTransferRequest):
    """Transfer multiple (or all) playlists from one service to another."""
    sm = deps.streaming_manager
    source_svc = sm.service(req.source_service)
    if not source_svc:
        raise HTTPException(status_code=400, detail=f"Source '{req.source_service}' not found")

    # Build search func for target
    if req.target_service == "local":
        async def search_func(query: str, limit: int):
            rows = await deps.db.fetchall(
                """SELECT t.id as source_id, t.title, t.artist_name,
                          a.title as album_title, t.duration_ms, t.isrc
                   FROM tracks t LEFT JOIN albums a ON a.id = t.album_id
                   WHERE t.title LIKE ? OR t.artist_name LIKE ?
                   LIMIT ?""",
                (f"%{query}%", f"%{query}%", limit),
            )
            return [dict(r) for r in rows]
    else:
        target_svc = sm.service(req.target_service)
        if not target_svc:
            raise HTTPException(status_code=400, detail=f"Target '{req.target_service}' not found")

        async def search_func(query: str, limit: int):
            results = await target_svc.search(query, limit=limit)
            return [
                {"title": r.title, "artist_name": r.artist, "source_id": r.id,
                 "duration_ms": getattr(r, "duration_ms", 0)}
                for r in results
            ]

    result = await batch_transfer(
        source_service_name=req.source_service,
        target_service_name=req.target_service,
        source_service=source_svc,
        search_func=search_func,
        db=deps.db,
        event_bus=deps.event_bus,
        playlist_ids=req.playlist_ids,
        match_threshold=req.match_threshold,
    )
    return BatchTransferResponse(
        batch_id=result["batch_id"],
        total_playlists=result["total_playlists"],
        status=result["status"],
    )


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

@router.post("/merge")
async def merge_playlists_endpoint(req: MergeRequest):
    """Merge multiple playlists into one, with optional deduplication."""
    result = await merge_playlists(
        playlist_sources=req.playlists,
        target_name=req.target_name,
        db=deps.db,
        streaming_manager=deps.streaming_manager,
        deduplicate=req.deduplicate,
    )
    return result


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@router.post("/export")
async def export_playlist_endpoint(req: ExportRequest):
    """Export a playlist to CSV, JSON, XSPF, or text."""
    sm = deps.streaming_manager

    if req.service == "local":
        row = await deps.db.fetchone(
            "SELECT name FROM playlists WHERE id = ?", (int(req.playlist_id),))
        if not row:
            raise HTTPException(status_code=404, detail="Playlist not found")
        name = row["name"]
        track_rows = await deps.db.fetchall(
            """SELECT t.title, t.artist_name, a.title as album_title,
                      t.duration_ms, t.source_id, t.isrc
               FROM playlist_tracks pt
               JOIN tracks t ON t.id = pt.track_id
               LEFT JOIN albums a ON a.id = t.album_id
               WHERE pt.playlist_id = ?
               ORDER BY pt.position""",
            (int(req.playlist_id),),
        )
        tracks = [dict(r) for r in track_rows]
    else:
        svc = sm.service(req.service)
        if not svc:
            raise HTTPException(status_code=400, detail=f"Service '{req.service}' not found")
        try:
            playlists = await svc.getUserPlaylists()
            name = next(
                (p.name for p in playlists if p.source_id == req.playlist_id),
                "Playlist",
            )
            raw_tracks = await svc.getPlaylistTracks(req.playlist_id)
            tracks = [
                {"title": t.get("title", ""), "artist_name": t.get("artist_name", ""),
                 "album_title": t.get("album_title", ""), "duration_ms": t.get("duration_ms", 0),
                 "isrc": t.get("isrc", "")}
                for t in raw_tracks
            ]
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

    content, content_type, filename = export_playlist(name, tracks, req.format)
    return PlainTextResponse(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

@router.post("/import")
async def import_playlist_endpoint(
    file: UploadFile = File(...),
    format: str = Query("csv"),
):
    """Import a playlist from a file (CSV, JSON, XSPF, text).
    Creates a local playlist with the imported tracks matched to the library."""
    content = (await file.read()).decode("utf-8")
    name, tracks = import_playlist(content, format)

    # Create local playlist
    await deps.db.execute(
        "INSERT INTO playlists (name, description) VALUES (?, ?)",
        (name, f"Imported from {file.filename}"),
    )
    await deps.db.commit()
    row = await deps.db.fetchone("SELECT last_insert_rowid() as id")
    playlist_id = row["id"]

    # Try to match tracks to local library
    matched = 0
    for i, t in enumerate(tracks):
        # Search by title + artist
        track_row = await deps.db.fetchone(
            """SELECT id FROM tracks
               WHERE title LIKE ? AND (artist_name LIKE ? OR ? = '')
               LIMIT 1""",
            (f"%{t.title}%", f"%{t.artist}%", t.artist),
        )
        if track_row:
            await deps.db.execute(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                (playlist_id, track_row["id"], i),
            )
            matched += 1
    await deps.db.commit()

    return {
        "playlist_id": playlist_id,
        "playlist_name": name,
        "total_tracks": len(tracks),
        "matched_to_library": matched,
        "unmatched": len(tracks) - matched,
    }

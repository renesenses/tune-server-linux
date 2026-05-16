"""API routes for importing library data from Roon, Plex, and playlist files."""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog
from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from tune_server.api.deps import deps
from tune_server.library.importer import (
    run_plex_import,
    run_playlist_import,
    run_roon_import,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/library/import", tags=["import"])

# Track background import tasks
_import_tasks: dict[str, asyncio.Task] = {}
_import_results: dict[str, dict] = {}

MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB


def _report_to_dict(report) -> dict:
    """Convert an ImportReport to a JSON-serialisable dict."""
    return {
        "source": report.source,
        "total_rows": report.total_rows,
        "matched": report.matched,
        "unmatched": report.unmatched,
        "play_counts_updated": report.play_counts_updated,
        "ratings_updated": report.ratings_updated,
        "history_entries_added": report.history_entries_added,
        "playlists_created": report.playlists_created,
        "details": report.details[:500],  # cap preview to 500 rows
    }


async def _read_upload(file: UploadFile, max_size: int = MAX_UPLOAD_SIZE) -> str:
    """Read an uploaded file as text, with size guard."""
    raw = await file.read()
    if len(raw) > max_size:
        raise HTTPException(
            status_code=413,
            detail=f"Fichier trop volumineux ({len(raw)} octets, max {max_size})",
        )
    # Try UTF-8, fall back to latin-1
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


# -----------------------------------------------------------------------
# Roon import
# -----------------------------------------------------------------------

@router.post("/roon")
async def import_roon(
    file: UploadFile = File(...),
    dry_run: bool = Query(False, alias="preview"),
    background: bool = Query(False),
):
    """Import library data from a Roon CSV export.

    - **preview=true**: dry-run, returns match report without writing.
    - **background=true**: runs import in background, returns task ID.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Nom de fichier manquant")

    content = await _read_upload(file)
    logger.info("import_roon_start", filename=file.filename, size=len(content), dry_run=dry_run)

    if background and not dry_run:
        task_id = f"roon_{id(content)}"

        async def _bg():
            try:
                report = await run_roon_import(content, dry_run=False)
                _import_results[task_id] = _report_to_dict(report)
            except Exception as exc:
                logger.error("import_roon_bg_error", error=str(exc))
                _import_results[task_id] = {"error": str(exc)}
            finally:
                _import_tasks.pop(task_id, None)

        _import_tasks[task_id] = asyncio.create_task(_bg())
        return {"status": "started", "task_id": task_id}

    report = await run_roon_import(content, dry_run=dry_run)
    return _report_to_dict(report)


# -----------------------------------------------------------------------
# Plex import
# -----------------------------------------------------------------------

@router.post("/plex")
async def import_plex(
    file: UploadFile = File(...),
    dry_run: bool = Query(False, alias="preview"),
    background: bool = Query(False),
):
    """Import library data from a Plex XML export.

    - **preview=true**: dry-run, returns match report without writing.
    - **background=true**: runs import in background, returns task ID.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Nom de fichier manquant")

    content = await _read_upload(file)
    logger.info("import_plex_start", filename=file.filename, size=len(content), dry_run=dry_run)

    if background and not dry_run:
        task_id = f"plex_{id(content)}"

        async def _bg():
            try:
                report = await run_plex_import(content, dry_run=False)
                _import_results[task_id] = _report_to_dict(report)
            except Exception as exc:
                logger.error("import_plex_bg_error", error=str(exc))
                _import_results[task_id] = {"error": str(exc)}
            finally:
                _import_tasks.pop(task_id, None)

        _import_tasks[task_id] = asyncio.create_task(_bg())
        return {"status": "started", "task_id": task_id}

    report = await run_plex_import(content, dry_run=dry_run)
    return _report_to_dict(report)


# -----------------------------------------------------------------------
# Playlist import (M3U / M3U8 / PLS)
# -----------------------------------------------------------------------

@router.post("/playlists")
async def import_playlists(
    file: UploadFile = File(...),
    dry_run: bool = Query(False, alias="preview"),
):
    """Import playlists from M3U, M3U8, or PLS files.

    Tracks are matched against the Tune library; a new playlist is created
    with the matched tracks.
    """
    filename = file.filename or "playlist.m3u"
    content = await _read_upload(file)
    logger.info("import_playlists_start", filename=filename, size=len(content), dry_run=dry_run)

    report = await run_playlist_import(content, filename, dry_run=dry_run)
    return _report_to_dict(report)


# -----------------------------------------------------------------------
# Preview / status helpers
# -----------------------------------------------------------------------

@router.get("/status/{task_id}")
async def import_status(task_id: str):
    """Check the status of a background import task."""
    if task_id in _import_tasks:
        return {"status": "running", "task_id": task_id}
    if task_id in _import_results:
        result = _import_results.pop(task_id)
        return {"status": "completed", "task_id": task_id, "result": result}
    raise HTTPException(status_code=404, detail="Tâche d'import introuvable")

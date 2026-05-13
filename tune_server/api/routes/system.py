from __future__ import annotations

import asyncio
import json
import os

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from tune_server.api.deps import deps
from tune_server.config import persist_env_var, settings
from tune_server.models import BackupInfo, MusicDirRequest, MusicDirsResponse, ScanStatusResponse, SystemConfigResponse, SystemHealthResponse, SystemStatsResponse

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
        spotify_enabled=settings.spotify_enabled,
        deezer_enabled=settings.deezer_enabled,
        discovery_enabled=settings.discovery_enabled,
        sync_poll_playing_interval=settings.sync_poll_playing_interval,
        sync_poll_idle_interval=settings.sync_poll_idle_interval,
        sync_drift_threshold_ms=settings.sync_drift_threshold_ms,
        sync_correction_cooldown_s=settings.sync_correction_cooldown_s,
        sync_dlna_default_buffer_s=settings.sync_dlna_default_buffer_s,
        db_engine=settings.db_engine,
        db_path=settings.db_path if settings.db_engine == "sqlite" else None,
        db_pool_min=settings.db_pool_min if settings.db_engine == "postgres" else None,
        db_pool_max=settings.db_pool_max if settings.db_engine == "postgres" else None,
        db_connected=deps.db is not None,
        resample_policy=settings.resample_policy,
        audio_buffer_kb=settings.audio_buffer_kb,
        prebuffer_seconds=settings.prebuffer_seconds,
        local_exclusive_mode=settings.local_exclusive_mode,
        local_latency_ms=settings.local_latency_ms,
        dsp_enabled=settings.dsp_enabled,
        dsp_filter=settings.dsp_filter,
        dsp_impulse_response=settings.dsp_impulse_response,
        dsp_sample_rate=settings.dsp_sample_rate,
        metadata_readonly=settings.metadata_readonly,
        discogs_token_set=bool(settings.discogs_token),
        enrich_on_scan=settings.enrich_on_scan,
    )


@router.patch("/config")
async def update_config(body: dict):
    """Update server configuration. Supports: metadata_readonly, enrich_on_scan."""
    updated = {}

    if "metadata_readonly" in body:
        val = bool(body["metadata_readonly"])
        settings.metadata_readonly = val
        persist_env_var("TUNE_METADATA_READONLY", str(val))
        updated["metadata_readonly"] = val

    if "enrich_on_scan" in body:
        val = bool(body["enrich_on_scan"])
        settings.enrich_on_scan = val
        persist_env_var("TUNE_ENRICH_ON_SCAN", str(val))
        updated["enrich_on_scan"] = val

    if not updated:
        raise HTTPException(status_code=400, detail="No valid configuration fields provided")

    return updated


@router.post("/database/test")
async def test_database():
    """Test the current database connection."""
    if not deps.db:
        return {"ok": False, "error": "Database not available"}
    try:
        row = await deps.db.fetchone("SELECT 1")
        return {"ok": row is not None, "engine": settings.db_engine}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/database/status")
async def database_status():
    """Get current database engine, stats, and capabilities."""
    if not deps.db:
        return {"engine": "none", "connected": False}

    engine = getattr(deps.db, "engine_name", "sqlite")
    stats = {}
    try:
        for table in ("tracks", "albums", "artists", "playlists", "zones", "radio_stations"):
            row = await deps.db.fetchone(f"SELECT COUNT(*) as cnt FROM {table}")
            stats[table] = row["cnt"] if row else 0
    except Exception:
        pass

    result = {
        "engine": engine,
        "connected": True,
        "stats": stats,
    }

    # SQLite-specific info
    if engine == "sqlite":
        db_path = getattr(settings, "db_path", "tune_server.db")
        result["path"] = db_path
        try:
            import os
            result["size_mb"] = round(os.path.getsize(db_path) / (1024 * 1024), 1)
        except Exception:
            pass

    # PostgreSQL-specific info
    if engine == "postgres":
        result["url"] = settings.db_url.split("@")[-1] if settings.db_url else None
        try:
            row = await deps.db.fetchone("SELECT pg_database_size(current_database()) as size")
            result["size_mb"] = round(row["size"] / (1024 * 1024), 1) if row else None
        except Exception:
            pass

    return result


class _MigrationRequest:
    pass


@router.post("/database/test-connection")
async def test_pg_connection(url: str = Query(..., description="PostgreSQL connection URL")):
    """Test a PostgreSQL connection before migrating."""
    try:
        import asyncpg
        conn = await asyncpg.connect(url)
        version = conn.get_server_version()
        await conn.close()
        return {"ok": True, "version": f"{version.major}.{version.minor}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/database/migrate", status_code=202)
async def migrate_database(
    target: str = Query(..., description="Target engine: 'postgres' or 'sqlite'"),
    url: str = Query(None, description="PostgreSQL connection URL (required if target=postgres)"),
):
    """Migrate database from current engine to target engine.

    This runs in the background. Check /database/status for progress.
    """
    current = getattr(deps.db, "engine_name", "sqlite")
    if current == target:
        raise HTTPException(400, f"Already using {target}")

    if target == "postgres" and not url:
        raise HTTPException(400, "url parameter required for postgres migration")

    # Test target connection first
    if target == "postgres":
        try:
            import asyncpg
            conn = await asyncpg.connect(url)
            await conn.close()
        except Exception as e:
            raise HTTPException(400, f"Cannot connect to PostgreSQL: {e}")

    # Run migration in background
    async def _do_migrate():
        import os
        os.environ["TUNE_DB_URL"] = url or ""
        from tune_server.db.migrate import migrate
        try:
            await migrate(current, target)
            # Update .env for persistence
            persist_env_var("TUNE_DB_ENGINE", target)
            if url:
                persist_env_var("TUNE_DB_URL", url)
        except Exception:
            import structlog
            structlog.get_logger().exception("migration_error")

    asyncio.create_task(_do_migrate())
    return {"status": "started", "from": current, "to": target}


@router.get("/duplicates-dir")
async def get_duplicates_dir():
    """Get duplicates directory info — size, file count."""
    from pathlib import Path
    dup_dir = Path(settings.duplicates_dir)
    if not dup_dir.exists():
        return {"path": str(dup_dir), "exists": False, "files": 0, "size_bytes": 0}
    files = list(dup_dir.rglob("*"))
    audio_files = [f for f in files if f.is_file()]
    total_size = sum(f.stat().st_size for f in audio_files)
    return {
        "path": str(dup_dir),
        "exists": True,
        "files": len(audio_files),
        "size_bytes": total_size,
    }


@router.post("/duplicates-dir/clear", status_code=200)
async def clear_duplicates_dir():
    """Delete all files in the duplicates directory."""
    import shutil
    from pathlib import Path
    dup_dir = Path(settings.duplicates_dir)
    if not dup_dir.exists():
        return {"cleared": False, "message": "Directory does not exist"}
    files = list(dup_dir.rglob("*"))
    audio_files = [f for f in files if f.is_file()]
    total_size = sum(f.stat().st_size for f in audio_files)
    shutil.rmtree(str(dup_dir))
    dup_dir.mkdir(parents=True, exist_ok=True)
    return {"cleared": True, "files_deleted": len(audio_files), "size_freed": total_size}


@router.post("/library/clear", status_code=200)
async def clear_library():
    """Clear all tracks, albums, and artists from the library. Keeps zones, playlists, radios."""
    await deps.db.execute("DELETE FROM tracks")
    await deps.db.execute("DELETE FROM albums")
    await deps.db.execute("DELETE FROM artists")
    await deps.db.commit()
    return {"cleared": True, "message": "Library cleared (tracks, albums, artists)"}


@router.post("/scan", status_code=202)
async def trigger_scan(
    path: Optional[str] = Query(None, description="Scan a single directory instead of all music_dirs"),
    full: bool = Query(False, description="Force full rescan of all files (re-read tags)"),
):
    if not deps.scanner:
        raise HTTPException(status_code=503, detail="Scanner not available")

    if deps.scanner.is_scanning:
        raise HTTPException(status_code=409, detail="Scan already in progress")

    if path:
        resolved = str(Path(path).resolve())
        resolved_dirs = [str(Path(d).resolve()) for d in settings.music_dirs]
        if not any(resolved == d or resolved.startswith(d + "/") for d in resolved_dirs):
            raise HTTPException(status_code=400, detail="Path is not under a configured music directory")
        scan_dirs = [resolved]
    else:
        scan_dirs = settings.music_dirs

    if full:
        await deps.db.execute("UPDATE tracks SET mtime = 0 WHERE mtime IS NOT NULL")
        await deps.db.commit()

    # Run scan in background
    task = asyncio.create_task(deps.scanner.scan(scan_dirs))
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    return {"status": "scan_started", "music_dirs": scan_dirs, "full": full}


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


@router.get("/backups", response_model=list[BackupInfo])
async def list_backups():
    if not deps.db:
        raise HTTPException(status_code=503, detail="Database not available")
    return deps.db.list_backups()


@router.post("/music-dirs", response_model=MusicDirsResponse)
async def add_music_dir(body: MusicDirRequest):
    resolved = str(Path(body.path).resolve())

    if not Path(resolved).is_dir():
        raise HTTPException(status_code=400, detail=f"Path does not exist or is not a directory on this server: {resolved}")

    # Prevent adding the duplicates directory as a music source
    dup_dir = str(Path(settings.duplicates_dir).resolve())
    if resolved == dup_dir or resolved.startswith(dup_dir + "/"):
        raise HTTPException(status_code=400, detail="Cannot add the duplicates directory as a music source")

    current = [str(Path(d).resolve()) for d in settings.music_dirs]
    if resolved in current:
        raise HTTPException(status_code=409, detail="Directory already configured")

    settings.music_dirs.append(resolved)
    persist_env_var("TUNE_MUSIC_DIRS", json.dumps(settings.music_dirs))

    # Restart filesystem watcher
    if deps.watcher:
        await deps.watcher.update_dirs(settings.music_dirs)

    # Trigger scan of the new directory
    if deps.scanner and not deps.scanner.is_scanning:
        task = asyncio.create_task(deps.scanner.scan([resolved]))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    return MusicDirsResponse(music_dirs=settings.music_dirs)


@router.delete("/music-dirs", response_model=MusicDirsResponse)
async def remove_music_dir(body: MusicDirRequest):
    resolved = str(Path(body.path).resolve())
    current = [str(Path(d).resolve()) for d in settings.music_dirs]

    if resolved not in current:
        raise HTTPException(status_code=404, detail="Directory not found in configuration")

    if len(settings.music_dirs) <= 1:
        raise HTTPException(status_code=400, detail="Cannot remove the last music directory")

    idx = current.index(resolved)
    settings.music_dirs.pop(idx)
    persist_env_var("TUNE_MUSIC_DIRS", json.dumps(settings.music_dirs))

    # Restart filesystem watcher
    if deps.watcher:
        await deps.watcher.update_dirs(settings.music_dirs)

    return MusicDirsResponse(music_dirs=settings.music_dirs)


@router.post("/backups", response_model=BackupInfo)
async def create_backup():
    if not deps.db:
        raise HTTPException(status_code=503, detail="Database not available")
    backup = deps.db.create_backup()
    if not backup:
        raise HTTPException(status_code=500, detail="Backup failed")
    return backup


@router.post("/backups/{filename}/restore")
async def restore_backup(filename: str):
    if not deps.db:
        raise HTTPException(status_code=503, detail="Database not available")
    success = await deps.db.restore_backup(filename)
    if not success:
        raise HTTPException(status_code=404, detail="Backup not found or restore failed")
    return {"restored": True, "filename": filename}


@router.get("/database/export")
async def export_database():
    """Export the full database as a file download.

    - SQLite: returns the .db file as-is (after a safety checkpoint).
    - PostgreSQL: returns a pg_dump stream (plain SQL).
    """
    if not deps.db:
        raise HTTPException(status_code=503, detail="Database not available")

    engine = getattr(deps.db, "engine_name", "sqlite")
    timestamp = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")

    if engine == "sqlite":
        db_path = Path(getattr(settings, "db_path", "tune_server.db"))
        if not db_path.exists():
            raise HTTPException(status_code=404, detail="Database file not found")

        # Checkpoint WAL into the main file to ensure the exported snapshot is complete
        try:
            await deps.db.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception:
            pass

        filename = f"tune_server_{timestamp}.db"
        return FileResponse(str(db_path), media_type="application/octet-stream", filename=filename)

    if engine == "postgres":
        import asyncio as _aio
        import shutil as _shutil

        pg_dump = _shutil.which("pg_dump")
        if not pg_dump:
            raise HTTPException(
                status_code=501,
                detail="pg_dump binary not found on the server — cannot export PostgreSQL. Install postgresql-client.",
            )

        db_url = getattr(settings, "db_url", None)
        if not db_url:
            raise HTTPException(status_code=500, detail="PostgreSQL URL not configured")

        async def _stream_dump():
            proc = await _aio.create_subprocess_exec(
                pg_dump, "--no-owner", "--no-acl", db_url,
                stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.PIPE,
            )
            assert proc.stdout is not None
            try:
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await proc.wait()

        filename = f"tune_server_{timestamp}.sql"
        return StreamingResponse(
            _stream_dump(),
            media_type="application/sql",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    raise HTTPException(status_code=501, detail=f"Export not supported for engine '{engine}'")


@router.post("/database/import")
async def import_database(file: UploadFile = File(...)):
    """Import a database file to replace the current one.

    A safety backup is created first. Caller must restart the server after a successful import.
    - SQLite: accepts a .db file (validated via SQLite magic bytes).
    - PostgreSQL: accepts a .sql dump (plain SQL) and applies it via psql.
    """
    if not deps.db:
        raise HTTPException(status_code=503, detail="Database not available")

    engine = getattr(deps.db, "engine_name", "sqlite")

    if engine == "sqlite":
        import os as _os

        db_path = Path(getattr(settings, "db_path", "tune_server.db"))

        # Save upload to a temp file in the same dir (atomic rename requires same filesystem)
        tmp_path = db_path.with_name(db_path.name + ".import.tmp")
        size = 0
        try:
            with tmp_path.open("wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    size += len(chunk)

            # Validate SQLite magic header
            with tmp_path.open("rb") as f:
                header = f.read(16)
            if not header.startswith(b"SQLite format 3\x00"):
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail="Uploaded file is not a valid SQLite database")

            # Create safety backup of current DB before swapping
            from tune_server.db import backup as _backup
            try:
                _backup.create_backup(str(db_path))
            except Exception:
                pass

            # Close current DB connection so the file can be replaced
            try:
                await deps.db.close()
            except Exception:
                pass

            # Remove WAL/SHM files (from old DB)
            for suffix in ("-wal", "-shm"):
                wal = db_path.with_name(db_path.name + suffix)
                if wal.exists():
                    wal.unlink()

            _os.replace(str(tmp_path), str(db_path))

            return {"imported": True, "engine": "sqlite", "size": size, "restart_required": True}
        except HTTPException:
            raise
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"Import failed: {e}")

    if engine == "postgres":
        import asyncio as _aio
        import shutil as _shutil
        import tempfile as _tempfile

        psql = _shutil.which("psql")
        if not psql:
            raise HTTPException(
                status_code=501,
                detail="psql binary not found on the server — cannot import into PostgreSQL. Install postgresql-client.",
            )

        db_url = getattr(settings, "db_url", None)
        if not db_url:
            raise HTTPException(status_code=500, detail="PostgreSQL URL not configured")

        # Save upload to temp file
        with _tempfile.NamedTemporaryFile(mode="wb", suffix=".sql", delete=False) as tmp:
            size = 0
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
                size += len(chunk)
            tmp_sql = tmp.name

        try:
            proc = await _aio.create_subprocess_exec(
                psql, db_url, "-f", tmp_sql,
                stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise HTTPException(status_code=400, detail=f"psql failed: {stderr.decode(errors='replace')[:500]}")
            return {"imported": True, "engine": "postgres", "size": size, "restart_required": True}
        finally:
            Path(tmp_sql).unlink(missing_ok=True)

    raise HTTPException(status_code=501, detail=f"Import not supported for engine '{engine}'")



# ── Remote mode management ──────────────────────────────────────────────

@router.get("/mode")
async def get_mode():
    """Get current server mode (server or remote)."""
    return {
        "mode": settings.mode,
        "remote_host": settings.remote_host,
    }


@router.post("/mode")
async def set_mode(request: dict):
    """Switch between server and remote mode. Requires restart."""
    mode = request.get("mode", "server")
    if mode not in ("server", "remote"):
        raise HTTPException(400, "mode must be 'server' or 'remote'")

    persist_env_var("TUNE_MODE", mode)

    if mode == "remote":
        host = request.get("remote_host")
        if host:
            persist_env_var("TUNE_REMOTE_HOST", host)

    return {
        "mode": mode,
        "remote_host": request.get("remote_host"),
        "message": "Restart Tune Server to apply",
    }


@router.post("/enrich", status_code=202)
async def trigger_enrich():
    """Trigger one immediate pass of the metadata enricher (MusicBrainz + Discogs artist images)."""
    if not deps.enricher:
        raise HTTPException(status_code=503, detail="Enricher not available")
    await deps.enricher.enrich_now()
    # Count artists without images
    row = await deps.db.fetchone(
        "SELECT COUNT(*) as cnt FROM artists WHERE image_path IS NULL OR image_path = ''"
    )
    remaining = row["cnt"] if row else 0
    return {"status": "started", "artists_without_image": remaining}


@router.get("/discover-servers")
async def discover_servers():
    """Discover Tune Servers on the LAN."""
    from tune_server.remote.discovery import discover_tune_servers
    servers = await discover_tune_servers(timeout=5)
    return [
        {"name": s.name, "host": s.host, "port": s.port, "uuid": s.uuid}
        for s in servers
    ]


# --- Update management ---

@router.get("/update/check")
async def check_update():
    """Check if a newer version is available on GitHub."""
    if not deps.update_checker:
        raise HTTPException(status_code=503, detail="Update checker not available")
    info = await deps.update_checker.check_for_update()
    source_install = deps.update_checker.is_source_install
    payload = info or {
        "current_version": deps.update_checker.current_version,
        "latest_version": None,
        "update_available": False,
    }
    payload["installable"] = not source_install
    if source_install:
        payload["install_hint"] = "Source install detected. Run `git pull && pip install -e .` then restart."
    return payload


@router.post("/update/install")
async def install_update():
    """Trigger an async download + install of the latest update.

    Returns immediately. The download runs in a background task; the
    client polls /update/status to know when the swap+restart is about
    to happen. Without this, the request blocks for the full download
    (30+ s for a ~100 MB Windows zip), browsers give up with
    'Failed to fetch', and the user sees an error even though the
    install actually succeeded server-side. (Reported by Jacques on
    Windows.)
    """
    import platform
    if not deps.update_checker:
        raise HTTPException(status_code=503, detail="Update checker not available")
    if deps.update_checker.is_source_install:
        raise HTTPException(
            status_code=409,
            detail="Source install detected. Run `git pull && pip install -e .` then restart the service.",
        )
    if not deps.update_checker.update_available:
        raise HTTPException(status_code=400, detail="No update available")

    # Mark started so the status endpoint can report progress.
    from datetime import datetime as _dt
    deps.update_checker._install_state = {
        "phase": "downloading",
        "version": deps.update_checker.latest_version,
        "started_at": _dt.utcnow().isoformat(),
    }

    is_windows = platform.system().lower() == "windows"

    async def _run_install_in_background():
        success = await deps.update_checker.download_and_install()
        if not success:
            deps.update_checker._install_state["phase"] = "failed"
            return
        if is_windows:
            deps.update_checker._spawn_windows_apply_helper()
            deps.update_checker._install_state["phase"] = "restarting"
            await asyncio.sleep(2)
            import os, signal
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            deps.update_checker._install_state["phase"] = "installed_restart_required"

    asyncio.create_task(_run_install_in_background())

    return {
        "status": "started",
        "version": deps.update_checker.latest_version,
        "windows_swap_pending": is_windows,
        "poll_url": "/api/v1/system/update/status",
    }


@router.get("/update/status")
async def update_status():
    """Poll the in-progress install state. Returns phase ∈
    {idle, downloading, restarting, installed_restart_required, failed}.
    """
    if not deps.update_checker:
        return {"phase": "idle"}
    state = getattr(deps.update_checker, "_install_state", None)
    if not state:
        return {"phase": "idle"}
    return state


@router.post("/restart")
async def restart_server():
    """Restart the server process. Returns immediately, server restarts after 2s."""
    import os
    import signal

    async def _delayed_restart():
        await asyncio.sleep(2)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_delayed_restart())
    return {"status": "restarting", "message": "Server will restart in 2 seconds"}


@router.post("/rescan")
async def rescan_library():
    """Trigger a full library rescan (all music directories)."""
    if not deps.scanner:
        raise HTTPException(503, "Scanner not available")
    asyncio.create_task(deps.scanner.scan())
    return {"status": "scanning"}


@router.post("/clear-cache")
async def clear_cache():
    """Clear artwork cache and temporary files."""
    import shutil
    cleared = 0
    cache_dir = Path(settings.data_dir) / "artwork_cache" if hasattr(settings, "data_dir") else None
    if cache_dir and cache_dir.is_dir():
        for f in cache_dir.iterdir():
            if f.is_file() and f.suffix in (".jpg", ".png", ".webp"):
                f.unlink()
                cleared += 1
    return {"cleared": cleared}


@router.get("/logs")
async def get_logs(lines: int = 100):
    """Get recent server logs."""
    import subprocess
    try:
        result = subprocess.run(
            ["journalctl", "-u", "tune-server", "--no-pager", "-n", str(lines)],
            capture_output=True, text=True, timeout=10,
        )
        return {"logs": result.stdout, "lines": lines}
    except FileNotFoundError:
        # Not a systemd system — try log file
        log_file = Path("/tmp/tune-server.log")
        if log_file.exists():
            text = log_file.read_text()
            log_lines = text.strip().split("\n")
            return {"logs": "\n".join(log_lines[-lines:]), "lines": len(log_lines[-lines:])}
        return {"logs": "", "lines": 0}


@router.get("/diagnostics/bundle")
async def diagnostics_bundle():
    """One-click diagnostic ZIP for testers.

    Bundles diagnostics.json + the recent log file + a masked copy of the
    runtime config. Designed so a non-technical Windows tester can hit a
    button in the web UI and email me the resulting file. NEVER include
    raw credentials, ARLs, OAuth tokens, or DB passwords.
    """
    import io
    import json as _json
    import re
    import sys
    import zipfile
    from datetime import datetime

    diag_json = await diagnostics(errors_limit=200)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("diagnostics.json", _json.dumps(diag_json, indent=2, default=str))

        # tune-server.log — same path the app teed stdout to. Search a few
        # likely candidates so we work whether the user is running from
        # source, from a PyInstaller bundle, or from a packager install.
        candidates = []
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).resolve().parent / "tune-server.log")
        candidates.append(Path.home() / ".tune" / "tune-server.log")
        candidates.append(Path.cwd() / "tune-server.log")
        for log_path in candidates:
            if log_path.is_file():
                try:
                    zf.write(log_path, arcname="tune-server.log")
                except Exception:
                    pass
                # Also bundle the rolled-over copy if any.
                rolled = log_path.with_suffix(log_path.suffix + ".1")
                if rolled.is_file():
                    try:
                        zf.write(rolled, arcname="tune-server.log.1")
                    except Exception:
                        pass
                break

        # Mask env-style secrets and embed for context.
        env_path = Path.cwd() / ".env"
        if env_path.is_file():
            try:
                raw = env_path.read_text(encoding="utf-8", errors="replace")
                masked_lines = []
                for line in raw.splitlines():
                    if "=" in line and not line.lstrip().startswith("#"):
                        key, _, val = line.partition("=")
                        if any(s in key.upper() for s in (
                            "TOKEN", "PASSWORD", "ARL", "SECRET", "CLIENT_ID",
                            "API_KEY", "AUTH", "DB_URL", "WEBHOOK",
                        )) and val.strip():
                            stripped = val.strip().strip('"').strip("'")
                            if len(stripped) > 6:
                                line = f"{key}={stripped[:3]}***{stripped[-3:]}"
                            else:
                                line = f"{key}=***"
                    masked_lines.append(line)
                zf.writestr(".env.masked", "\n".join(masked_lines))
            except Exception:
                pass

    buf.seek(0)
    fname = f"tune-diagnostics-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/diagnostics")
async def diagnostics(errors_limit: int = Query(50, le=200)):
    """Full system diagnostics — single JSON for remote debugging.

    Designed for testers: a single ``curl /api/v1/system/diagnostics`` gives
    me everything I need to triage their issue without ssh/journalctl
    access — version, schema drift, recent errors, last scan stats,
    streaming auth state, output health.
    """
    import platform
    import sys
    import os

    from tune_server import __version__
    from tune_server.utils.error_buffer import recent_errors

    db_engine = settings.db_engine if hasattr(settings, "db_engine") else "sqlite"

    diag: dict = {
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pid": os.getpid(),
        "uptime_seconds": None,
        "memory_mb": None,
        "cpu_count": os.cpu_count(),
        "db": _db_diagnostics(db_engine),
        "schema_drift": await _schema_drift(),
        "music_dirs": settings.music_dirs,
        "zones_count": len(deps.zone_manager.list_zones()) if deps.zone_manager else 0,
        "streaming_services": _streaming_diagnostics(),
        "last_scan": _scan_diagnostics(),
        "outputs_health": _outputs_diagnostics(),
        "recent_errors": recent_errors(errors_limit),
    }

    # Uptime / memory (best-effort, requires psutil)
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        import time
        diag["uptime_seconds"] = int(time.time() - proc.create_time())
        diag["memory_mb"] = round(proc.memory_info().rss / 1024 / 1024, 1)
    except ImportError:
        pass

    return diag


def _db_diagnostics(engine: str) -> dict:
    """DB metadata + schema-drift report (columns in SA model not in live)."""
    info: dict = {"engine": engine}
    try:
        if engine == "sqlite":
            db_path = getattr(settings, "db_path", "tune_server.db")
            info["path"] = db_path
            try:
                size = os.path.getsize(db_path)
                info["size_bytes"] = size
            except OSError:
                info["size_bytes"] = None
        else:
            # Mask credentials in PG URL
            url = getattr(settings, "db_url", "") or ""
            if "@" in url:
                info["host"] = url.split("@", 1)[1]  # everything after user:pass@
            info["url_masked"] = True
    except Exception as e:
        info["error"] = str(e)

    return info


async def _schema_drift() -> list[dict]:
    """Columns declared in SA metadata but missing from the live DB.

    Always empty after v0.7.17 auto-migration; non-empty means something
    blocked the migration and the user is going to hit 500s.
    """
    drift: list[dict] = []
    try:
        from sqlalchemy import inspect as sa_inspect
        from tune_server.db.tables import metadata as sa_metadata
        sa_engine = getattr(deps.db, "sa_engine", None)
        if sa_engine is None:
            return drift

        def _live(sync_conn):
            insp = sa_inspect(sync_conn)
            return {
                t: {col["name"] for col in insp.get_columns(t)}
                for t in insp.get_table_names()
            }

        async with sa_engine.begin() as conn:
            live_cols = await conn.run_sync(_live)

        for table in sa_metadata.tables.values():
            if table.name not in live_cols:
                continue
            missing = {c.name for c in table.columns} - live_cols[table.name]
            if missing:
                drift.append({"table": table.name, "missing_columns": sorted(missing)})
    except Exception as e:
        return [{"error": str(e)}]
    return drift


def _streaming_diagnostics() -> dict:
    """Per-service: enabled, authenticated, last error if any."""
    result: dict = {}
    for name, svc in deps.streaming_services.items():
        entry: dict = {"enabled": True}
        try:
            entry["authenticated"] = bool(svc.is_authenticated)
        except Exception as e:
            # Don't let one broken service tank the whole endpoint (the bug
            # we hit on .15 with the suspended Tidal account).
            entry["authenticated"] = False
            entry["auth_error"] = str(e)[:200]
        result[name] = entry
    return result


def _scan_diagnostics() -> dict | None:
    if deps.scanner is None:
        return None
    return {
        "scanning": deps.scanner.is_scanning,
        "last_scan_at": getattr(deps.scanner, "last_scan_at", None),
        "last_scan_stats": getattr(deps.scanner, "last_scan_stats", None),
    }


def _outputs_diagnostics() -> dict:
    """How many devices were seen recently per discovery channel."""
    out: dict = {"dlna": 0, "airplay": 0}
    if deps.discovery_manager is None:
        return out
    try:
        if getattr(deps.discovery_manager, "ssdp", None):
            out["dlna"] = len(deps.discovery_manager.ssdp.devices)
        if getattr(deps.discovery_manager, "mdns", None):
            out["airplay"] = len(deps.discovery_manager.mdns._devices)
    except Exception as e:
        out["error"] = str(e)
    return out

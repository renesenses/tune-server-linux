from __future__ import annotations

import asyncio
import json
import os

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

import structlog

from tune_server.api.deps import deps
from tune_server.config import persist_env_var, settings
from tune_server.models import BackupInfo, MusicDirRequest, MusicDirsResponse, OutputType, ScanStatusResponse, SystemConfigResponse, SystemHealthResponse, SystemStatsResponse

logger = structlog.get_logger()

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

    if "local_exclusive_mode" in body:
        val = bool(body["local_exclusive_mode"])
        settings.local_exclusive_mode = val
        persist_env_var("TUNE_LOCAL_EXCLUSIVE_MODE", str(val))
        updated["local_exclusive_mode"] = val

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

    # Run scan in background — pass force_rescan so the scanner re-reads
    # every file's metadata even when mtime/size haven't changed.
    task = asyncio.create_task(deps.scanner.scan(scan_dirs, force_rescan=full))
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    return {"status": "scan_started", "music_dirs": scan_dirs, "full": full}


@router.get("/scan/status", response_model=ScanStatusResponse)
async def scan_status():
    return ScanStatusResponse(scanning=deps.scanner.is_scanning if deps.scanner else False)


@router.get("/scan/schedule")
async def get_scan_schedule():
    """Get the current scheduled scan configuration."""
    scheduler = getattr(deps, "scan_scheduler", None)
    if not scheduler:
        return {"enabled": False, "time": None, "next_run": None}
    return scheduler.status()


@router.post("/scan/schedule")
async def set_scan_schedule(body: dict = {}):
    """Set or disable the daily library scan schedule.

    Body: ``{"time": "03:00"}`` to enable, ``{"enabled": false}`` to disable.
    """
    scheduler = getattr(deps, "scan_scheduler", None)
    if not scheduler:
        raise HTTPException(status_code=503, detail="Scan scheduler not available")

    enabled = body.get("enabled", True)
    time_str = body.get("time")

    if not enabled:
        await scheduler.set_schedule(enabled=False)
        return scheduler.status()

    if not time_str:
        raise HTTPException(status_code=400, detail="'time' field required (format HH:MM)")

    try:
        await scheduler.set_schedule(time_str=time_str, enabled=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return scheduler.status()


@router.get("/stats/listening")
async def listening_stats():
    """Lightweight listening statistics dashboard.

    Returns total plays, total listening hours, top artists, top albums,
    recent plays, and plays-by-day trend. All from the playback_history table.
    """
    if not deps.db:
        raise HTTPException(status_code=503, detail="Database not available")

    # Total plays & duration
    totals_row = await deps.db.fetchone(
        "SELECT COUNT(*) as total_plays, COALESCE(SUM(listened_ms), 0) as total_ms "
        "FROM playback_history"
    )
    total_plays = totals_row["total_plays"] if totals_row else 0
    total_duration_hours = round((totals_row["total_ms"] if totals_row else 0) / 3_600_000, 1)

    # Top artists
    top_artists_rows = await deps.db.fetchall(
        "SELECT artist_name as name, COUNT(*) as plays "
        "FROM playback_history "
        "WHERE artist_name IS NOT NULL AND artist_name != '' "
        "GROUP BY artist_name ORDER BY plays DESC LIMIT 20"
    )
    top_artists = [{"name": r["name"], "plays": r["plays"]} for r in top_artists_rows]

    # Top albums
    top_albums_rows = await deps.db.fetchall(
        "SELECT album_title as title, artist_name, MAX(cover_path) as cover_path, "
        "COUNT(*) as plays "
        "FROM playback_history "
        "WHERE album_title IS NOT NULL AND album_title != '' "
        "GROUP BY album_title, artist_name ORDER BY plays DESC LIMIT 20"
    )
    top_albums = [
        {"title": r["title"], "artist_name": r["artist_name"],
         "cover_path": r["cover_path"], "plays": r["plays"]}
        for r in top_albums_rows
    ]

    # Recent plays
    recent_rows = await deps.db.fetchall(
        "SELECT track_title, artist_name, album_title, cover_path, "
        "played_at, source, zone_id "
        "FROM playback_history ORDER BY played_at DESC LIMIT 20"
    )
    recent = [
        {"track_title": r["track_title"], "artist_name": r["artist_name"],
         "album_title": r["album_title"], "cover_path": r["cover_path"],
         "played_at": r["played_at"], "source": r["source"],
         "zone_id": r["zone_id"]}
        for r in recent_rows
    ]

    # Plays by day (last 30 days)
    plays_by_day_rows = await deps.db.fetchall(
        "SELECT DATE(played_at) as date, COUNT(*) as count "
        "FROM playback_history "
        "WHERE played_at > datetime('now', '-30 days') "
        "GROUP BY DATE(played_at) ORDER BY date"
    )
    plays_by_day = [{"date": r["date"], "count": r["count"]} for r in plays_by_day_rows]

    return {
        "total_plays": total_plays,
        "total_duration_hours": total_duration_hours,
        "top_artists": top_artists,
        "top_albums": top_albums,
        "recent": recent,
        "plays_by_day": plays_by_day,
    }


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
    homebrew_install = deps.update_checker.is_homebrew_install
    macos_dmg = deps.update_checker.is_macos_dmg_install
    payload["installable"] = not source_install
    payload["homebrew"] = homebrew_install
    payload["macos_dmg"] = macos_dmg
    if source_install:
        payload["install_hint"] = "Source install detected. Run `git pull && pip install -e .` then restart."
    elif homebrew_install:
        payload["install_hint"] = "Homebrew install detected. Update will run `brew upgrade tune-server`."
    elif macos_dmg:
        payload["install_hint"] = (
            "macOS app detected. The update will be downloaded to ~/Downloads. "
            "Open the DMG and drag Tune Server to Applications to complete the update."
        )
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
    is_macos = platform.system().lower() == "darwin"

    async def _run_install_in_background():
        success = await deps.update_checker.download_and_install()
        if not success:
            deps.update_checker._install_state["phase"] = "failed"
            return
        if is_macos:
            # macOS: download_and_install already set phase to "dmg_ready".
            # Do NOT SIGTERM — the user drag-installs from the DMG.
            pass
        elif is_windows:
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


@router.post("/update/apply")
async def apply_update():
    """Download the latest update DMG to ~/Downloads and open it (macOS only).

    For macOS DMG installs: downloads the new version DMG, moves it to
    ~/Downloads, and opens it in Finder. The user must drag the app to
    /Applications manually (macOS codesign requires this — silent
    replacement would break the signature).

    On other platforms, use POST /update/install instead.
    """
    import platform

    if not deps.update_checker:
        raise HTTPException(status_code=503, detail="Update checker not available")

    if platform.system().lower() != "darwin":
        raise HTTPException(
            status_code=400,
            detail="This endpoint is macOS-only. Use POST /update/install for other platforms.",
        )

    if deps.update_checker.is_source_install:
        raise HTTPException(
            status_code=409,
            detail="Source install detected. Run `git pull && pip install -e .` then restart.",
        )

    if not deps.update_checker.update_available:
        raise HTTPException(status_code=400, detail="No update available")

    # If the DMG was already downloaded by a previous call, return its path
    # without re-downloading.
    existing_dmg = deps.update_checker.dmg_download_path
    if existing_dmg and existing_dmg.is_file():
        return {
            "status": "ready",
            "phase": "dmg_ready",
            "version": deps.update_checker.latest_version,
            "dmg_path": str(existing_dmg),
            "message": "DMG already downloaded. Open it and drag Tune Server to Applications.",
        }

    # Start the download in background (same as /update/install but
    # semantically dedicated to macOS apply-and-notify flow).
    from datetime import datetime as _dt
    deps.update_checker._install_state = {
        "phase": "downloading",
        "version": deps.update_checker.latest_version,
        "started_at": _dt.utcnow().isoformat(),
    }

    async def _download_dmg():
        success = await deps.update_checker.download_and_install()
        if not success:
            deps.update_checker._install_state["phase"] = "failed"

    asyncio.create_task(_download_dmg())

    return {
        "status": "started",
        "phase": "downloading",
        "version": deps.update_checker.latest_version,
        "poll_url": "/api/v1/system/update/status",
        "message": "Downloading DMG to ~/Downloads. Poll /update/status for progress.",
    }


@router.get("/update/status")
async def update_status():
    """Poll the in-progress install state. Returns phase ∈
    {idle, downloading, restarting, installed_restart_required, dmg_ready, failed}.
    """
    if not deps.update_checker:
        return {"phase": "idle"}
    state = getattr(deps.update_checker, "_install_state", None)
    if not state:
        return {"phase": "idle"}
    return state


@router.post("/restart")
async def restart_server():
    """Restart the server process. Returns immediately, server restarts after 2s.

    On Linux (systemd): SIGTERM triggers exit; Restart=always relaunches.
    On macOS: SIGTERM triggers exit; the launcher relaunches.
    On Windows (PyInstaller frozen): writes a small helper bat that waits
    for the current process to fully exit (port released, file handles
    closed), then relaunches via start-tune-server.bat or tune-server.exe.
    Without this, the bat watchdog sees exit code 0 as a clean shutdown
    and does NOT restart.
    """
    import os
    import platform
    import signal
    import sys

    is_windows_frozen = (
        platform.system().lower() == "windows"
        and getattr(sys, "frozen", False)
    )

    async def _delayed_restart():
        await asyncio.sleep(2)

        if is_windows_frozen:
            _spawn_windows_restart_helper()

        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_delayed_restart())
    return {"status": "restarting", "message": "Server will restart in 2 seconds"}


def _spawn_windows_restart_helper() -> None:
    """Spawn a detached helper bat that waits for the current process to
    exit, then relaunches Tune Server.

    The helper polls tasklist until tune-server.exe is gone (max 30 s,
    force-kills if stuck), then launches start-tune-server.bat (or
    tune-server.exe directly if the bat is missing). Mirrors the pattern
    used by the update applier in updater.py.
    """
    import subprocess
    import sys

    if not getattr(sys, "frozen", False):
        return

    exe_dir = Path(sys.executable).resolve().parent
    install_dir = str(exe_dir)

    helper_script = f"""@echo off
setlocal enabledelayedexpansion
cd /d "{install_dir}"

REM Wait for tune-server.exe to fully exit and release port 8888.
set /a TRIES=0
:wait_exit
tasklist /FI "IMAGENAME eq tune-server.exe" /FO CSV /NH 2>nul | findstr /I "tune-server.exe" >nul
if errorlevel 1 goto :proc_gone
set /a TRIES+=1
if !TRIES! GEQ 15 (
    taskkill /IM tune-server.exe /F >nul 2>&1
    ping -n 4 127.0.0.1 >nul
    goto :proc_gone
)
ping -n 3 127.0.0.1 >nul
goto :wait_exit
:proc_gone

REM Grace period for Windows to release port and file handles.
REM 4 seconds (ping -n 5 = 4 intervals) is enough for TIME_WAIT to clear.
ping -n 5 127.0.0.1 >nul

if exist "start-tune-server.bat" (
    start "" "{install_dir}\\start-tune-server.bat"
) else (
    start "" "{install_dir}\\tune-server.exe"
)

endlocal
exit /b 0
"""
    helper = exe_dir / "_restart.bat"
    try:
        helper.write_text(helper_script, encoding="ascii")
    except Exception:
        logger.exception("restart_helper_write_failed")
        return

    try:
        # DETACHED_PROCESS (0x08) | CREATE_NEW_PROCESS_GROUP (0x200)
        CREATE_FLAGS = 0x00000008 | 0x00000200
        subprocess.Popen(
            ["cmd.exe", "/c", str(helper)],
            cwd=install_dir,
            creationflags=CREATE_FLAGS,
            close_fds=True,
        )
        logger.info("restart_helper_spawned", path=str(helper))
    except Exception:
        logger.exception("restart_helper_spawn_failed")


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


@router.post("/cleanup")
async def cleanup_server():
    """Full server cleanup: orphan albums/artists, stale history, artwork cache, DB vacuum."""
    results = {}

    # 1. Delete orphan albums (no tracks)
    try:
        orphan_albums = await deps.album_repo.delete_orphans()
        results["orphan_albums_deleted"] = orphan_albums
    except Exception as e:
        results["orphan_albums_error"] = str(e)

    # 2. Delete orphan artists (no tracks, no albums)
    try:
        orphan_count = 0
        all_artists = await deps.artist_repo.all(limit=50000)
        for artist in all_artists:
            tracks = await deps.track_repo.list_by_artist(artist.id, limit=1) if hasattr(deps.track_repo, "list_by_artist") else []
            albums = await deps.album_repo.list_by_artist(artist.id, limit=1) if hasattr(deps.album_repo, "list_by_artist") else []
            if not tracks and not albums:
                await deps.artist_repo.delete(artist.id)
                orphan_count += 1
        results["orphan_artists_deleted"] = orphan_count
    except Exception as e:
        results["orphan_artists_error"] = str(e)

    # 3. Clean stale artwork cache (files not referenced by any album)
    try:
        cache_dir = Path(settings.artwork_cache_dir)
        if cache_dir.is_dir():
            all_covers = set()
            tracks_sample = await deps.track_repo.list(limit=50000)
            for t in tracks_sample:
                if t.cover_path:
                    cover_file = Path(t.cover_path).name if "/" in (t.cover_path or "") else t.cover_path
                    all_covers.add(cover_file)
            stale = 0
            for f in cache_dir.iterdir():
                if f.is_file() and f.name not in all_covers:
                    f.unlink()
                    stale += 1
            results["stale_artwork_deleted"] = stale
    except Exception as e:
        results["stale_artwork_error"] = str(e)

    # 4. DB vacuum (SQLite only)
    try:
        if settings.db_engine == "sqlite":
            await deps.db.execute("VACUUM")
            results["db_vacuumed"] = True
        else:
            results["db_vacuumed"] = False
    except Exception as e:
        results["db_vacuum_error"] = str(e)

    # 5. Clear playback history older than 90 days
    try:
        from datetime import datetime, timedelta
        cutoff = (datetime.utcnow() - timedelta(days=90)).isoformat()
        deleted = await deps.db.execute(
            "DELETE FROM playback_history WHERE played_at < ?", (cutoff,)
        )
        results["old_history_deleted"] = getattr(deleted, "rowcount", 0) if deleted else 0
    except Exception as e:
        results["old_history_error"] = str(e)

    logger.info("server_cleanup_completed", **results)
    return results


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
        pass
    for candidate in [
        Path.home() / "Library" / "Logs" / "Tune Server.log",
        Path("/tmp/tune-server.log"),
        Path("/var/log/tune-server.log"),
        Path("/usr/local/var/log/tune-server.log"),
    ]:
        if candidate.exists():
            text = candidate.read_text()
            log_lines = text.strip().split("\n")
            return {"logs": "\n".join(log_lines[-lines:]), "lines": len(log_lines[-lines:])}
    return {"logs": "", "lines": 0}


@router.get("/config/export")
async def export_config():
    """Export server configuration as JSON.

    Includes zones, streaming auth status (NOT tokens), playlists, and settings.
    Designed for backup/migration between instances.
    """
    config: dict = {"version": 1}

    # Zones
    zones_data: list[dict] = []
    if deps.zone_manager:
        for z in deps.zone_manager.list_zones():
            zones_data.append({
                "name": z.name,
                "output_type": z.output_type.value,
                "output_device_id": z.output_device_id,
                "volume": z.player.volume,
                "sync_delay_ms": z.sync_delay_ms,
            })
    config["zones"] = zones_data

    # Streaming auth status (NOT tokens/credentials)
    streaming_status: dict = {}
    for name, svc in deps.streaming_services.items():
        try:
            streaming_status[name] = {"authenticated": bool(svc.is_authenticated)}
        except Exception:
            streaming_status[name] = {"authenticated": False}
    config["streaming_services"] = streaming_status

    # Playlists
    playlists_data: list[dict] = []
    if deps.playlist_repo:
        try:
            playlists = await deps.playlist_repo.list()
            for p in playlists:
                playlists_data.append({
                    "name": p.get("name", "") if isinstance(p, dict) else getattr(p, "name", ""),
                    "track_count": p.get("track_count", 0) if isinstance(p, dict) else getattr(p, "track_count", 0),
                })
        except Exception:
            pass
    config["playlists"] = playlists_data

    # Settings
    config["settings"] = {
        "music_dirs": settings.music_dirs,
        "api_port": settings.api_port,
        "stream_port": settings.stream_port,
        "resample_policy": settings.resample_policy,
        "crossfade_enabled": settings.crossfade_enabled,
        "crossfade_duration": settings.crossfade_duration,
        "enrich_on_scan": settings.enrich_on_scan,
        "metadata_readonly": settings.metadata_readonly,
    }

    return config


@router.post("/config/import")
async def import_config(body: dict):
    """Import server configuration from JSON.

    Recreates zones and applies settings. Streaming auth is skipped for
    security (tokens are never exported).
    """
    if not deps.zone_manager:
        raise HTTPException(status_code=503, detail="Zone manager not available")

    imported: dict = {"zones_created": 0, "settings_applied": []}

    # Import zones
    zones_data = body.get("zones", [])
    for z_data in zones_data:
        name = z_data.get("name")
        if not name:
            continue
        # Check if zone with this name already exists
        existing = [z for z in deps.zone_manager.list_zones() if z.name == name]
        if existing:
            continue  # Skip duplicates
        try:
            output_type = OutputType(z_data.get("output_type", "local"))
            await deps.zone_manager.create_zone(
                name=name,
                output_type=output_type,
                output_device_id=z_data.get("output_device_id"),
            )
            imported["zones_created"] += 1
        except Exception as e:
            logger.warning("config_import_zone_failed", name=name, error=str(e))

    # Import settings
    imported_settings = body.get("settings", {})
    if "music_dirs" in imported_settings:
        for d in imported_settings["music_dirs"]:
            if d not in settings.music_dirs and Path(d).is_dir():
                settings.music_dirs.append(d)
                imported["settings_applied"].append(f"music_dir:{d}")

    for key in ("enrich_on_scan", "metadata_readonly"):
        if key in imported_settings:
            setattr(settings, key, imported_settings[key])
            imported["settings_applied"].append(key)

    return imported


@router.get("/diagnostics/network")
async def network_diagnostics():
    """Network auto-diagnostic — multicast, renderers, DNS, internet."""
    import socket

    result: dict = {}

    # Multicast support (SSDP bind test)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass  # SO_REUSEPORT not available on Windows
        sock.settimeout(1)
        sock.bind(("", 1900))
        mreq = socket.inet_aton("239.255.255.250") + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.close()
        result["multicast_supported"] = True
    except Exception as e:
        result["multicast_supported"] = False
        result["multicast_error"] = str(e)

    # Discovered renderers
    renderers: list[dict] = []
    if deps.discovery_manager:
        for d in deps.discovery_manager.list_devices():
            renderers.append({
                "id": d.id,
                "name": d.name,
                "type": d.type.value if hasattr(d.type, "value") else str(d.type),
                "host": d.host,
            })
    result["discovered_renderers"] = renderers

    # Port 8888 reachable from localhost
    try:
        sock = socket.create_connection(("127.0.0.1", settings.api_port), timeout=2)
        sock.close()
        result["port_reachable"] = True
    except Exception:
        result["port_reachable"] = False
    result["port"] = settings.api_port

    # DNS resolution for streaming domains
    dns_results: dict[str, bool] = {}
    for domain in ("tidal.com", "api.qobuz.com", "api.deezer.com"):
        try:
            socket.getaddrinfo(domain, 443, socket.AF_INET, socket.SOCK_STREAM)
            dns_results[domain] = True
        except Exception:
            dns_results[domain] = False
    result["dns_resolution"] = dns_results

    # Internet connectivity
    try:
        sock = socket.create_connection(("1.1.1.1", 443), timeout=3)
        sock.close()
        result["internet_available"] = True
    except Exception:
        result["internet_available"] = False

    return result


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

    web_version = None
    web_index = Path(settings.web_dir or "") / "index.html" if settings.web_dir else None
    if web_index and web_index.is_file():
        import re
        html = web_index.read_text(encoding="utf-8", errors="ignore")[:2000]
        m = re.search(r'content="tune-version:([\d.]+)"', html)
        if m:
            web_version = m.group(1)

    diag: dict = {
        "version": __version__,
        "web_version": web_version or __version__,
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
        "tracks_count": await deps.track_repo.count() if deps.track_repo else 0,
        "albums_count": await deps.album_repo.count() if deps.album_repo else 0,
        "artists_count": await deps.artist_repo.count() if deps.artist_repo else 0,
        "radios_count": await deps.radio_repo.count() if deps.radio_repo else 0,
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

    # ffmpeg version
    diag["ffmpeg_version"] = _ffmpeg_version()
    diag["ffmpeg_path"] = settings.ffmpeg_path

    # ffprobe available
    import shutil
    diag["ffprobe_available"] = shutil.which(settings.ffprobe_path) is not None or Path(settings.ffprobe_path).is_file()

    # Disk free on DB partition
    diag["disk_free_mb"] = _disk_free_mb()

    # Audio devices (optional sounddevice)
    diag["audio_devices"] = _list_audio_devices()

    # Network interfaces (non-loopback)
    diag["network_interfaces"] = _list_network_interfaces()

    # Music dirs status
    diag["music_dirs_status"] = _music_dirs_status()

    # First-run detection: no tracks scanned yet
    diag["first_run"] = diag["tracks_count"] == 0

    # Self-service hints for common issues (Windows testers)
    hints: list[str] = []
    if diag["first_run"] and not settings.music_dirs:
        hints.append("No music directory configured. Go to Settings in the web UI to add one.")
    if diag["first_run"] and diag["tracks_count"] == 0 and settings.music_dirs:
        for ms in diag.get("music_dirs_status", []):
            if not ms.get("exists"):
                hints.append(f"Music directory does not exist: {ms['path']}")
            elif ms.get("file_count", 0) == 0:
                hints.append(f"Music directory is empty: {ms['path']}")
    if diag.get("ffmpeg_version") is None:
        hints.append("FFmpeg not found. Streaming services require FFmpeg for transcoding.")
    if platform.system() == "Windows":
        out_health = diag.get("outputs_health", {})
        if out_health.get("dlna", 0) == 0 and out_health.get("airplay", 0) == 0:
            hints.append(
                "No audio renderers discovered. Check that Windows Firewall allows "
                "tune-server.exe on your private network (UDP 1900 for DLNA, UDP 5353 for AirPlay)."
            )
    diag["hints"] = hints
    diag["faq_url"] = "https://mozaiklabs.fr/faq"

    return diag


def _ffmpeg_version() -> str | None:
    """Run ``ffmpeg -version`` and return the first line."""
    try:
        import subprocess
        result = subprocess.run(
            [settings.ffmpeg_path, "-version"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout.splitlines()[0]
    except Exception:
        pass
    return None


def _disk_free_mb() -> int | None:
    """Free disk space (MB) on the partition containing the DB file."""
    try:
        import shutil
        db_path = getattr(settings, "db_path", "tune_server.db")
        stat = shutil.disk_usage(Path(db_path).parent)
        return int(stat.free / (1024 * 1024))
    except Exception:
        return None


def _list_audio_devices() -> list[dict] | None:
    """List local audio output devices via sounddevice (optional)."""
    try:
        import sounddevice as sd
        devices = []
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_output_channels", 0) > 0:
                devices.append({
                    "id": i,
                    "name": d["name"],
                    "channels": d["max_output_channels"],
                    "sample_rate": int(d.get("default_samplerate", 0)),
                })
        return devices
    except Exception:
        return None


def _list_network_interfaces() -> list[dict]:
    """List non-loopback network interfaces with their IP addresses."""
    result: list[dict] = []
    try:
        import socket
        import psutil
        addrs = psutil.net_if_addrs()
        for iface, addr_list in addrs.items():
            for addr in addr_list:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    result.append({"name": iface, "ip": addr.address})
    except ImportError:
        # psutil not available — fallback
        try:
            import socket
            hostname = socket.gethostname()
            ips = socket.getaddrinfo(hostname, None, socket.AF_INET)
            for info in ips:
                ip = info[4][0]
                if not ip.startswith("127."):
                    result.append({"name": "unknown", "ip": ip})
        except Exception:
            pass
    except Exception:
        pass
    return result


def _music_dirs_status() -> list[dict]:
    """Check each configured music_dir: exists, readable, file count."""
    statuses: list[dict] = []
    for music_dir in settings.music_dirs:
        p = Path(music_dir)
        exists = p.is_dir()
        readable = False
        file_count = 0
        if exists:
            readable = os.access(str(p), os.R_OK)
            try:
                file_count = sum(1 for _ in p.rglob("*") if _.is_file())
            except Exception:
                file_count = -1
        statuses.append({
            "path": music_dir,
            "exists": exists,
            "readable": readable,
            "file_count": file_count,
        })
    return statuses


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


# ── Audio health check (onboarding wizard) ───────────────────────────────


@router.get("/audio-check")
async def audio_check():
    """Quick audio health check for onboarding wizard."""
    zones = deps.zone_manager.list_zones() if deps.zone_manager else []
    devices = deps.discovery_manager.list_devices() if deps.discovery_manager else []

    # Check local audio outputs
    local_devices: list[dict] = []
    try:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_output_channels", 0) > 0:
                local_devices.append({
                    "id": i,
                    "name": d["name"],
                    "channels": d["max_output_channels"],
                    "default": i == sd.default.device[1],
                })
    except Exception:
        pass

    # Network renderers (DLNA, AirPlay, Chromecast, BluOS)
    renderer_types = {"dlna", "airplay", "chromecast", "bluos"}
    renderers = [
        d for d in devices
        if (d.type.value if hasattr(d.type, "value") else str(d.type)) in renderer_types
    ]

    return {
        "zones": len(zones),
        "zones_with_output": sum(
            1 for z in zones
            if z.output_type != OutputType.LOCAL or local_devices
        ),
        "local_outputs": local_devices,
        "network_renderers": [
            {"id": r.id, "name": r.name, "type": r.type.value if hasattr(r.type, "value") else str(r.type)}
            for r in renderers
        ],
        "has_audio": len(zones) > 0 and (len(local_devices) > 0 or len(renderers) > 0),
        "issues": _detect_audio_issues(zones, local_devices, renderers),
    }


# ── Plugin management ──────────────────────────────────────────────────


@router.get("/plugins")
async def list_plugins():
    """List all loaded and failed plugins with status and metadata."""
    if not deps.plugin_loader:
        return []
    return deps.plugin_loader.list_plugins()


@router.get("/plugins/{name}")
async def get_plugin(name: str):
    """Get detailed info for a single plugin."""
    if not deps.plugin_loader:
        raise HTTPException(status_code=503, detail="Plugin system not available")

    # Check active plugins
    plugin = deps.plugin_loader.plugins.get(name)
    if plugin:
        config_cls = None
        config_fields: list[dict] = []
        try:
            config_cls = plugin.config_schema()
            if config_cls:
                for field_name, field_info in config_cls.model_fields.items():
                    config_fields.append({
                        "name": field_name,
                        "type": str(field_info.annotation) if field_info.annotation else "str",
                        "default": repr(field_info.default) if field_info.default is not None else None,
                        "description": field_info.description or "",
                    })
        except Exception:
            pass

        return {
            "name": name,
            "version": getattr(plugin, "version", "?"),
            "description": getattr(plugin, "description", ""),
            "status": "active",
            "output_types": deps.plugin_loader._registered_output_types.get(name, []),
            "routes": deps.plugin_loader._registered_routes.get(name, []),
            "hooks_count": deps.plugin_loader._registered_hooks.get(name, 0),
            "config_schema": config_fields,
        }

    # Check failed/disabled plugins
    error = deps.plugin_loader.failed.get(name)
    if error is not None:
        return {
            "name": name,
            "version": "?",
            "description": "",
            "status": "disabled" if error == "disabled" else "error",
            "error": error if error != "disabled" else None,
            "output_types": [],
            "routes": [],
            "hooks_count": 0,
            "config_schema": [],
        }

    raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")


@router.post("/plugins/{name}/disable")
async def disable_plugin(name: str):
    """Disable a plugin. It will be skipped on next server boot."""
    if not deps.db:
        raise HTTPException(status_code=503, detail="Database not available")

    # Verify plugin exists (active or already failed)
    known = set(deps.plugin_loader.plugins.keys()) | set(deps.plugin_loader.failed.keys()) if deps.plugin_loader else set()
    if name not in known:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")

    token_data = json.dumps({"enabled": False})
    await deps.db.execute(
        "INSERT INTO streaming_auth (service, token_data) VALUES (:service, :token_data) "
        "ON CONFLICT(service) DO UPDATE SET token_data = :token_data, updated_at = CURRENT_TIMESTAMP",
        {"service": f"plugin_{name}", "token_data": token_data},
    )
    await deps.db.commit()
    return {"name": name, "enabled": False, "message": "Plugin will be skipped on next boot"}


@router.post("/plugins/{name}/enable")
async def enable_plugin(name: str):
    """Re-enable a disabled plugin. Takes effect on next server boot."""
    if not deps.db:
        raise HTTPException(status_code=503, detail="Database not available")

    token_data = json.dumps({"enabled": True})
    await deps.db.execute(
        "INSERT INTO streaming_auth (service, token_data) VALUES (:service, :token_data) "
        "ON CONFLICT(service) DO UPDATE SET token_data = :token_data, updated_at = CURRENT_TIMESTAMP",
        {"service": f"plugin_{name}", "token_data": token_data},
    )
    await deps.db.commit()
    return {"name": name, "enabled": True, "message": "Plugin will be loaded on next boot"}


# ── Health monitor endpoints ───────────────────────────────────────────


@router.get("/health/monitor")
async def health_monitor():
    """Live health status from the background health monitor."""
    monitor = getattr(deps, "health_monitor", None)
    if not monitor:
        return {
            "status": "ok",
            "uptime_seconds": 0,
            "checks": {},
            "alerts": [],
        }
    return {
        "status": monitor.status,
        "uptime_seconds": monitor.uptime_seconds,
        "checks": monitor.checks,
        "alerts": monitor.alerts,
    }


@router.get("/health/alerts")
async def health_alerts():
    """Recent health alerts only."""
    monitor = getattr(deps, "health_monitor", None)
    if not monitor:
        return []
    return monitor.alerts


def _detect_audio_issues(
    zones: list, local_devices: list[dict], renderers: list,
) -> list[dict]:
    issues: list[dict] = []
    if not zones:
        issues.append({
            "code": "no_zones",
            "message": "Aucune zone configurée",
            "severity": "error",
        })
    if not local_devices and not renderers:
        issues.append({
            "code": "no_outputs",
            "message": "Aucune sortie audio détectée",
            "severity": "error",
        })
    if zones and not any(
        hasattr(z, "player") and z.player is not None for z in zones
    ):
        issues.append({
            "code": "zones_no_player",
            "message": "Zones sans lecteur actif",
            "severity": "warning",
        })
    return issues

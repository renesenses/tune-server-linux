"""Auto-sync scheduler — runs periodic playlist syncs based on sync_interval_minutes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from tune_server.playlist_manager.sync import sync_playlist

logger = structlog.get_logger()


def _parse_ts(value) -> Optional[datetime]:
    """Parse an ISO8601 timestamp or datetime from the DB. Returns a naive UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        s = str(value).replace("Z", "").replace("T", " ")
        # Strip timezone info if present (e.g., "2026-01-01 12:00:00+00:00")
        if "+" in s:
            s = s.split("+")[0]
        return datetime.fromisoformat(s.strip())
    except (ValueError, TypeError):
        return None


class AutoSyncScheduler:
    """Background worker that polls playlist_links and triggers sync when due.

    Checks every `check_interval_seconds` (default 60s) whether any link with
    sync_interval_minutes > 0 is due for a sync (last_synced_at + interval < now).
    """

    def __init__(
        self,
        db,
        streaming_manager,
        check_interval_seconds: int = 60,
    ) -> None:
        self._db = db
        self._sm = streaming_manager
        self._check_interval = check_interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="playlist_autosync")
        logger.info("playlist_autosync_started", check_interval=self._check_interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        # Initial short delay so it doesn't fire at startup before everything's ready
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=10)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("playlist_autosync_tick_error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._check_interval)
                return  # Stop requested
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        # Find all links with an auto-sync interval configured
        try:
            rows = await self._db.fetchall(
                """SELECT id, local_playlist_id, service, service_playlist_id,
                          sync_direction, last_synced_at, sync_interval_minutes
                   FROM playlist_links
                   WHERE sync_interval_minutes IS NOT NULL
                     AND sync_interval_minutes > 0"""
            )
        except Exception:
            # Column may not exist yet on old DBs — skip silently
            return

        now = datetime.utcnow()
        for row in rows:
            interval = row["sync_interval_minutes"] or 0
            if interval <= 0:
                continue
            last = _parse_ts(row["last_synced_at"])
            if last is not None and now < last + timedelta(minutes=interval):
                continue  # Not due yet

            await self._sync_link(dict(row))

    async def _sync_link(self, link: dict) -> None:
        service = link["service"]
        svc = self._sm.service(service) if self._sm else None
        if not svc or not svc.is_authenticated:
            logger.debug("playlist_autosync_skip_unauth", link_id=link["id"], service=service)
            return

        async def search_func(query: str, limit: int = 10):
            try:
                results = await svc.search(query, limit=limit)
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
            except Exception:
                return []

        try:
            await sync_playlist(
                link=link,
                db=self._db,
                streaming_service=svc,
                search_func=search_func,
            )
            # Update last_synced_at
            await self._db.execute(
                "UPDATE playlist_links SET last_synced_at = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), link["id"]),
            )
            await self._db.commit()
            logger.info("playlist_autosync_done", link_id=link["id"], service=service)
        except Exception:
            logger.exception("playlist_autosync_error", link_id=link["id"])

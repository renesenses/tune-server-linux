"""Scheduled library scan — runs a full scan at a configured daily time."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from tune_server.db.engine import Database
    from tune_server.library.scanner import LibraryScanner

logger = structlog.get_logger()


class ScanScheduler:
    """Run a library scan once per day at a configured time (HH:MM).

    The schedule is persisted in the ``streaming_auth`` table under the
    service key ``"scan_schedule"`` so it survives restarts without needing
    schema changes.  ``token_data`` stores a JSON string like
    ``{"time": "03:00", "enabled": true}``.
    """

    def __init__(
        self,
        db: Database,
        scanner: LibraryScanner,
        music_dirs: list[str],
        initial_time: str | None = None,
    ) -> None:
        self._db = db
        self._scanner = scanner
        self._music_dirs = music_dirs
        self._enabled = False
        self._time_str: str | None = initial_time  # "HH:MM"
        self._task: asyncio.Task | None = None
        self._next_run: datetime | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load persisted schedule from DB and arm the timer."""
        await self._load_from_db()
        if self._enabled and self._time_str:
            self._arm()
            logger.info("scan_scheduler_started", time=self._time_str,
                        next_run=self._next_run.isoformat() if self._next_run else None)

    async def stop(self) -> None:
        self._cancel()
        logger.info("scan_scheduler_stopped")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def time_str(self) -> str | None:
        return self._time_str

    @property
    def next_run(self) -> datetime | None:
        return self._next_run

    async def set_schedule(self, time_str: str | None = None, enabled: bool = True) -> None:
        """Update schedule. Pass ``enabled=False`` or ``time_str=None`` to disable."""
        if not enabled or not time_str:
            self._enabled = False
            self._time_str = None
            self._cancel()
            await self._persist()
            logger.info("scan_scheduler_disabled")
            return

        # Validate HH:MM
        _parse_time(time_str)
        self._enabled = True
        self._time_str = time_str
        self._cancel()
        self._arm()
        await self._persist()
        logger.info("scan_scheduler_updated", time=time_str,
                    next_run=self._next_run.isoformat() if self._next_run else None)

    def status(self) -> dict:
        return {
            "enabled": self._enabled,
            "time": self._time_str,
            "next_run": self._next_run.isoformat() if self._next_run else None,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _arm(self) -> None:
        """Schedule the next scan task."""
        if not self._time_str:
            return
        hour, minute = _parse_time(self._time_str)
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        self._next_run = target
        delay = (target - now).total_seconds()
        self._task = asyncio.create_task(self._wait_and_scan(delay))

    def _cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._next_run = None

    async def _wait_and_scan(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            logger.info("scan_scheduler_triggered", time=self._time_str)
            start = datetime.now()
            try:
                await self._scanner.scan(self._music_dirs)
                duration = (datetime.now() - start).total_seconds()
                logger.info("scan_scheduler_completed", duration_s=round(duration, 1))
            except Exception:
                logger.exception("scan_scheduler_scan_error")
            # Re-arm for next day
            self._arm()
        except asyncio.CancelledError:
            pass

    async def _load_from_db(self) -> None:
        import json
        try:
            row = await self._db.fetchone(
                "SELECT token_data FROM streaming_auth WHERE service = ?",
                ("scan_schedule",),
            )
            if row:
                data = json.loads(row["token_data"])
                self._enabled = data.get("enabled", False)
                self._time_str = data.get("time")
        except Exception:
            logger.debug("scan_scheduler_no_saved_schedule")

    async def _persist(self) -> None:
        import json
        data = json.dumps({"enabled": self._enabled, "time": self._time_str})
        try:
            await self._db.execute(
                """INSERT INTO streaming_auth (service, token_data) VALUES (?, ?)
                   ON CONFLICT(service) DO UPDATE SET token_data = ?, updated_at = CURRENT_TIMESTAMP""",
                ("scan_schedule", data, data),
            )
            await self._db.commit()
        except Exception:
            logger.exception("scan_scheduler_persist_error")


def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' and return (hour, minute). Raises ValueError on bad format."""
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: {time_str!r} (expected HH:MM)")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time: {time_str!r}")
    return hour, minute

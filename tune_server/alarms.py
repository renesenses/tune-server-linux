"""Alarm scheduler — wake-up / scheduled playback with holiday support."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from tune_server.db.engine import Database

logger = structlog.get_logger()


def _easter(year: int) -> date:
    """Compute Easter Sunday using the anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def get_holidays(year: int, country: str = "FR") -> set[date]:
    """Return public holidays for a given year and country."""
    if country != "FR":
        return set()
    easter = _easter(year)
    return {
        date(year, 1, 1),    # Jour de l'An
        easter + timedelta(days=1),   # Lundi de Pâques
        date(year, 5, 1),    # Fête du Travail
        date(year, 5, 8),    # Victoire 1945
        easter + timedelta(days=39),  # Ascension
        easter + timedelta(days=50),  # Lundi de Pentecôte
        date(year, 7, 14),   # Fête nationale
        date(year, 8, 15),   # Assomption
        date(year, 11, 1),   # Toussaint
        date(year, 11, 11),  # Armistice
        date(year, 12, 25),  # Noël
    }


class AlarmScheduler:
    """Background scheduler that triggers playback at configured times."""

    def __init__(self, db: Database, play_callback) -> None:
        self._db = db
        self._play = play_callback
        self._task: asyncio.Task | None = None
        self._running = False
        self._fired_today: set[int] = set()
        self._last_date: date | None = None

    async def start(self) -> None:
        await self._ensure_table()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("alarm_scheduler_started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("alarm_scheduler_stopped")

    async def _ensure_table(self) -> None:
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS alarms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                time TEXT NOT NULL,
                days TEXT DEFAULT '1,2,3,4,5',
                skip_holidays INTEGER DEFAULT 0,
                holiday_country TEXT DEFAULT 'FR',
                zone_id INTEGER,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_name TEXT,
                volume INTEGER DEFAULT 50,
                fade_in_seconds INTEGER DEFAULT 30,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._db.commit()

    async def _loop(self) -> None:
        while self._running:
            try:
                now = datetime.now()
                today = now.date()

                if self._last_date != today:
                    self._fired_today.clear()
                    self._last_date = today

                rows = await self._db.fetchall(
                    "SELECT * FROM alarms WHERE enabled = 1"
                )

                day_of_week = today.isoweekday() % 7  # 0=Sun, 1=Mon..6=Sat

                for alarm in rows:
                    alarm_id = alarm["id"]
                    if alarm_id in self._fired_today:
                        continue

                    alarm_days = [int(d) for d in (alarm["days"] or "1,2,3,4,5").split(",") if d.strip()]
                    if day_of_week not in alarm_days:
                        continue

                    if alarm["skip_holidays"]:
                        country = alarm["holiday_country"] or "FR"
                        if today in get_holidays(today.year, country):
                            logger.info("alarm_skipped_holiday", alarm_id=alarm_id, date=str(today))
                            self._fired_today.add(alarm_id)
                            continue

                    alarm_time = alarm["time"]  # HH:MM
                    try:
                        h, m = map(int, alarm_time.split(":"))
                    except ValueError:
                        continue

                    if now.hour == h and now.minute == m:
                        self._fired_today.add(alarm_id)
                        logger.info("alarm_triggered",
                                    alarm_id=alarm_id, name=alarm["name"],
                                    zone_id=alarm["zone_id"],
                                    source=f"{alarm['source_type']}:{alarm['source_id']}")
                        try:
                            await self._play(
                                zone_id=alarm["zone_id"],
                                source_type=alarm["source_type"],
                                source_id=alarm["source_id"],
                                volume=alarm["volume"],
                                fade_in=alarm["fade_in_seconds"],
                            )
                        except Exception:
                            logger.exception("alarm_play_error", alarm_id=alarm_id)

            except Exception:
                logger.exception("alarm_scheduler_error")

            await asyncio.sleep(30)

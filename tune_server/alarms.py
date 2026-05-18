"""Alarm scheduler — wake-up / scheduled playback with fade-in, snooze, and day-of-week."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Coroutine

import structlog

if TYPE_CHECKING:
    from tune_server.db.engine import Database

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# French public holidays
# ---------------------------------------------------------------------------


def _easter(year: int) -> date:
    """Compute Easter Sunday using the anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
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


# ---------------------------------------------------------------------------
# Day-of-week helpers
# ---------------------------------------------------------------------------

# Canonical mapping: 0=Mon .. 6=Sun (ISO weekday - 1)
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Default: weekdays only
DEFAULT_DAYS = "0,1,2,3,4"  # Mon-Fri


def parse_days(days_str: str | None) -> list[int]:
    """Parse a comma-separated day string into a sorted list of ints (0=Mon..6=Sun).

    Accepts numeric ('0,1,2') or named ('mon,wed,fri') or special keywords:
      'daily'    -> [0,1,2,3,4,5,6]
      'weekdays' -> [0,1,2,3,4]
      'weekends' -> [5,6]
    """
    if not days_str:
        return list(range(5))  # weekdays

    days_str = days_str.strip().lower()
    if days_str == "daily":
        return list(range(7))
    if days_str == "weekdays":
        return list(range(5))
    if days_str == "weekends":
        return [5, 6]

    result: list[int] = []
    for part in days_str.split(","):
        part = part.strip()
        if part in DAY_NAMES:
            result.append(DAY_NAMES.index(part))
        else:
            try:
                val = int(part)
                if 0 <= val <= 6:
                    result.append(val)
            except ValueError:
                continue
    return sorted(set(result))


# ---------------------------------------------------------------------------
# Snooze tracker
# ---------------------------------------------------------------------------


class _SnoozeState:
    """Tracks snoozed alarms with their wake-up times."""

    def __init__(self) -> None:
        self._snoozed: dict[int, datetime] = {}  # alarm_id -> fire_at

    def snooze(self, alarm_id: int, minutes: int = 5) -> datetime:
        fire_at = datetime.now() + timedelta(minutes=minutes)
        self._snoozed[alarm_id] = fire_at
        return fire_at

    def pop_ready(self, now: datetime) -> list[int]:
        """Return alarm IDs whose snooze has elapsed, removing them."""
        ready = [aid for aid, t in self._snoozed.items() if now >= t]
        for aid in ready:
            del self._snoozed[aid]
        return ready

    def cancel(self, alarm_id: int) -> bool:
        return self._snoozed.pop(alarm_id, None) is not None

    def is_snoozed(self, alarm_id: int) -> bool:
        return alarm_id in self._snoozed

    def info(self, alarm_id: int) -> dict | None:
        t = self._snoozed.get(alarm_id)
        if not t:
            return None
        return {"alarm_id": alarm_id, "fires_at": t.isoformat(), "remaining_s": max(0, int((t - datetime.now()).total_seconds()))}


# ---------------------------------------------------------------------------
# Fade-in helper
# ---------------------------------------------------------------------------


async def fade_in_volume(
    set_volume: Callable[[float], Coroutine],
    target: float,
    duration_s: int,
    steps_per_second: int = 2,
) -> None:
    """Gradually increase volume from 0 to *target* over *duration_s* seconds."""
    if duration_s <= 0:
        await set_volume(target)
        return

    total_steps = max(1, duration_s * steps_per_second)
    step_delay = duration_s / total_steps

    for i in range(total_steps + 1):
        vol = (i / total_steps) * target
        await set_volume(vol)
        await asyncio.sleep(step_delay)


# ---------------------------------------------------------------------------
# Alarm Scheduler
# ---------------------------------------------------------------------------

# Type alias for the play callback
PlayCallback = Callable[..., Coroutine[Any, Any, None]]


class AlarmScheduler:
    """Background scheduler that triggers playback at configured times.

    Supports:
    - Day-of-week scheduling (Mon-Sun), daily, weekdays, weekends, one-shot
    - Zone selection (falls back to first available zone)
    - Source selection: radio, playlist, album, artist
    - Configurable fade-in volume ramp
    - Snooze (re-fire after N minutes)
    - Holiday skipping (FR public holidays)
    """

    def __init__(self, db: Database, play_callback: PlayCallback) -> None:
        self._db = db
        self._play = play_callback
        self._task: asyncio.Task | None = None
        self._running = False
        self._fired_today: set[int] = set()
        self._last_date: date | None = None
        self._snooze = _SnoozeState()
        self._fade_tasks: dict[int, asyncio.Task] = {}  # alarm_id -> running fade task

    # -- public API ----------------------------------------------------------

    async def start(self) -> None:
        await self._ensure_table()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("alarm_scheduler_started")

    async def stop(self) -> None:
        self._running = False
        # Cancel any running fade tasks
        for task in self._fade_tasks.values():
            task.cancel()
        self._fade_tasks.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("alarm_scheduler_stopped")

    async def snooze(self, alarm_id: int, minutes: int = 5) -> dict:
        """Snooze an alarm for *minutes* minutes.

        Returns snooze info dict or error.
        """
        minutes = max(1, min(60, minutes))
        row = await self._db.fetchone("SELECT * FROM alarms WHERE id = ?", (alarm_id,))
        if not row:
            return {"error": "alarm not found"}

        # Cancel any running fade for this alarm
        fade_task = self._fade_tasks.pop(alarm_id, None)
        if fade_task and not fade_task.done():
            fade_task.cancel()

        fire_at = self._snooze.snooze(alarm_id, minutes)
        # Remove from fired set so it can re-trigger
        self._fired_today.discard(alarm_id)
        logger.info("alarm_snoozed", alarm_id=alarm_id, minutes=minutes, fires_at=fire_at.isoformat())
        return {"alarm_id": alarm_id, "snoozed_minutes": minutes, "fires_at": fire_at.isoformat()}

    def snooze_info(self, alarm_id: int) -> dict | None:
        return self._snooze.info(alarm_id)

    # -- DB schema -----------------------------------------------------------

    async def _ensure_table(self) -> None:
        try:
            await self._db.fetchall("SELECT 1 FROM alarms LIMIT 1")
        except Exception:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS alarms ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name TEXT NOT NULL, "
                "time TEXT NOT NULL, "
                "days TEXT DEFAULT '0,1,2,3,4', "
                "one_shot INTEGER DEFAULT 0, "
                "skip_holidays INTEGER DEFAULT 0, "
                "holiday_country TEXT DEFAULT 'FR', "
                "zone_id INTEGER, "
                "source_type TEXT NOT NULL, "
                "source_id TEXT NOT NULL, "
                "source_name TEXT, "
                "volume INTEGER DEFAULT 50, "
                "fade_duration_s INTEGER DEFAULT 60, "
                "enabled INTEGER DEFAULT 1, "
                "last_fired_at TEXT, "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            await self._db.commit()
            return

        # Migrate: add columns that may be missing from older schemas
        await self._migrate_columns()

    async def _migrate_columns(self) -> None:
        """Add columns introduced after initial schema."""
        migrations = [
            ("one_shot", "INTEGER DEFAULT 0"),
            ("fade_duration_s", "INTEGER DEFAULT 60"),
            ("last_fired_at", "TEXT"),
        ]
        for col_name, col_def in migrations:
            try:
                await self._db.execute(f"SELECT {col_name} FROM alarms LIMIT 1")
            except Exception:
                try:
                    await self._db.execute(f"ALTER TABLE alarms ADD COLUMN {col_name} {col_def}")
                    await self._db.commit()
                    logger.info("alarm_table_migrated", column=col_name)
                except Exception:
                    pass  # column might already exist in another path

        # Rename fade_in_seconds -> fade_duration_s if old column exists
        try:
            await self._db.execute("SELECT fade_in_seconds FROM alarms LIMIT 1")
            # Old column exists — copy values to new column and keep both for compat
            try:
                await self._db.execute(
                    "UPDATE alarms SET fade_duration_s = fade_in_seconds "
                    "WHERE fade_duration_s IS NULL OR fade_duration_s = 60"
                )
                await self._db.commit()
            except Exception:
                pass
        except Exception:
            pass  # old column doesn't exist, nothing to migrate

    # -- main loop -----------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            try:
                now = datetime.now()
                today = now.date()

                # Reset daily fired set at midnight
                if self._last_date != today:
                    self._fired_today.clear()
                    self._last_date = today

                # Check snoozed alarms first
                snoozed_ready = self._snooze.pop_ready(now)
                for alarm_id in snoozed_ready:
                    row = await self._db.fetchone(
                        "SELECT * FROM alarms WHERE id = ? AND enabled = 1",
                        (alarm_id,),
                    )
                    if row:
                        self._fired_today.add(alarm_id)
                        await self._fire_alarm(row)

                # Check scheduled alarms
                rows = await self._db.fetchall(
                    "SELECT * FROM alarms WHERE enabled = 1"
                )

                # day_of_week: 0=Mon..6=Sun (ISO weekday - 1)
                dow = today.weekday()

                for alarm in rows:
                    alarm_id = alarm["id"]
                    if alarm_id in self._fired_today:
                        continue
                    if self._snooze.is_snoozed(alarm_id):
                        continue

                    alarm_days = parse_days(alarm.get("days"))
                    if dow not in alarm_days:
                        continue

                    if alarm.get("skip_holidays"):
                        country = alarm.get("holiday_country") or "FR"
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
                        await self._fire_alarm(alarm)

            except Exception:
                logger.exception("alarm_scheduler_error")

            await asyncio.sleep(30)

    async def _fire_alarm(self, alarm: dict) -> None:
        """Fire a single alarm: start playback then fade in."""
        alarm_id = alarm["id"]
        volume = alarm.get("volume", 50)
        fade_s = alarm.get("fade_duration_s") or alarm.get("fade_in_seconds") or 60
        zone_id = alarm.get("zone_id")
        source_type = alarm.get("source_type", "radio")
        source_id = alarm.get("source_id", "")

        logger.info(
            "alarm_triggered",
            alarm_id=alarm_id,
            name=alarm.get("name"),
            zone_id=zone_id,
            source=f"{source_type}:{source_id}",
            fade_s=fade_s,
        )

        try:
            await self._play(
                zone_id=zone_id,
                source_type=source_type,
                source_id=source_id,
                volume=volume,
                fade_in=fade_s,
            )
        except Exception:
            logger.exception("alarm_play_error", alarm_id=alarm_id)

        # Update last_fired_at
        try:
            await self._db.execute(
                "UPDATE alarms SET last_fired_at = ? WHERE id = ?",
                (datetime.now().isoformat(), alarm_id),
            )
            await self._db.commit()
        except Exception:
            pass

        # One-shot: disable after firing
        if alarm.get("one_shot"):
            try:
                await self._db.execute(
                    "UPDATE alarms SET enabled = 0, updated_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), alarm_id),
                )
                await self._db.commit()
                logger.info("alarm_one_shot_disabled", alarm_id=alarm_id)
            except Exception:
                pass

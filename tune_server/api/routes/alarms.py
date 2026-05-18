"""Alarm management API routes.

Endpoints:
    GET    /api/v1/alarm            — list all alarms
    POST   /api/v1/alarm            — create alarm
    PUT    /api/v1/alarm/{id}       — update alarm
    DELETE /api/v1/alarm/{id}       — delete alarm
    POST   /api/v1/alarm/{id}/snooze — snooze a triggered alarm
    GET    /api/v1/alarm/holidays/{year} — list public holidays
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps as _deps
from tune_server.models import (
    AlarmCreate,
    AlarmResponse,
    AlarmSnoozeRequest,
    AlarmUpdate,
)

router = APIRouter(prefix="/alarm", tags=["alarm"])


def _row_to_alarm(row) -> dict:
    """Convert a DB row to an AlarmResponse-compatible dict."""
    d = dict(row)
    # Normalize boolean fields
    for key in ("one_shot", "skip_holidays", "enabled"):
        if key in d:
            d[key] = bool(d[key])
    # Normalize fade column name (old schema used fade_in_seconds)
    if "fade_in_seconds" in d and "fade_duration_s" not in d:
        d["fade_duration_s"] = d.pop("fade_in_seconds")
    elif "fade_in_seconds" in d:
        d.pop("fade_in_seconds", None)
    # Defaults for columns that may be missing in old rows
    d.setdefault("one_shot", False)
    d.setdefault("fade_duration_s", 60)
    d.setdefault("last_fired_at", None)
    return d


# ---- Legacy compat: also mount on /alarms (plural) ----
# The old routes used /alarms; keep them working via a second router
# that is NOT registered here (the main router uses /alarm singular).
# We simply add aliases at the end of this file.

legacy_router = APIRouter(prefix="/alarms", tags=["alarm"])


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("/", response_model=list[AlarmResponse])
async def list_alarms():
    """List all alarms."""
    deps = _deps
    rows = await deps.db.fetchall("SELECT * FROM alarms ORDER BY time")
    return [_row_to_alarm(r) for r in rows]


@router.post("/", response_model=AlarmResponse)
async def create_alarm(body: AlarmCreate):
    """Create a new alarm.

    **days**: comma-separated day numbers (0=Mon..6=Sun), or keywords
    ``daily``, ``weekdays``, ``weekends``.

    **source_type**: ``radio``, ``playlist``, ``album``, ``artist``.
    """
    deps = _deps
    now = datetime.now().isoformat()
    await deps.db.execute(
        "INSERT INTO alarms (name, time, days, one_shot, skip_holidays, holiday_country, "
        "zone_id, source_type, source_id, source_name, volume, fade_duration_s, "
        "enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            body.name,
            body.time,
            body.days,
            1 if body.one_shot else 0,
            1 if body.skip_holidays else 0,
            body.holiday_country,
            body.zone_id,
            body.source_type,
            body.source_id,
            body.source_name,
            body.volume,
            body.fade_duration_s,
            1 if body.enabled else 0,
            now,
            now,
        ),
    )
    await deps.db.commit()
    row = await deps.db.fetchone("SELECT * FROM alarms ORDER BY id DESC LIMIT 1")
    return _row_to_alarm(row)


@router.put("/{alarm_id}", response_model=AlarmResponse)
async def update_alarm(alarm_id: int, body: AlarmUpdate):
    """Update an alarm. Only provided fields are changed."""
    deps = _deps
    existing = await deps.db.fetchone("SELECT * FROM alarms WHERE id = ?", (alarm_id,))
    if not existing:
        raise HTTPException(status_code=404, detail="Alarm not found")

    sets: list[str] = []
    vals: list = []

    field_map = {
        "name": "name",
        "time": "time",
        "days": "days",
        "one_shot": "one_shot",
        "skip_holidays": "skip_holidays",
        "holiday_country": "holiday_country",
        "zone_id": "zone_id",
        "source_type": "source_type",
        "source_id": "source_id",
        "source_name": "source_name",
        "volume": "volume",
        "fade_duration_s": "fade_duration_s",
        "enabled": "enabled",
    }

    for py_field, db_col in field_map.items():
        val = getattr(body, py_field, None)
        if val is None:
            continue
        if py_field in ("one_shot", "skip_holidays", "enabled"):
            val = 1 if val else 0
        sets.append(f"{db_col} = ?")
        vals.append(val)

    if sets:
        sets.append("updated_at = ?")
        vals.append(datetime.now().isoformat())
        vals.append(alarm_id)
        await deps.db.execute(
            f"UPDATE alarms SET {', '.join(sets)} WHERE id = ?", tuple(vals)
        )
        await deps.db.commit()

    row = await deps.db.fetchone("SELECT * FROM alarms WHERE id = ?", (alarm_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Alarm not found")
    return _row_to_alarm(row)


@router.delete("/{alarm_id}")
async def delete_alarm(alarm_id: int):
    """Delete an alarm."""
    deps = _deps
    existing = await deps.db.fetchone("SELECT * FROM alarms WHERE id = ?", (alarm_id,))
    if not existing:
        raise HTTPException(status_code=404, detail="Alarm not found")
    await deps.db.execute("DELETE FROM alarms WHERE id = ?", (alarm_id,))
    await deps.db.commit()
    return {"deleted": alarm_id}


# ---------------------------------------------------------------------------
# Snooze
# ---------------------------------------------------------------------------


@router.post("/{alarm_id}/snooze")
async def snooze_alarm(alarm_id: int, body: AlarmSnoozeRequest | None = None):
    """Snooze a triggered alarm.

    Delays the alarm by *minutes* (default 5, max 60).
    The alarm will re-fire after the snooze period.
    """
    deps = _deps
    if not deps.alarm_scheduler:
        raise HTTPException(status_code=503, detail="Alarm scheduler not running")

    minutes = body.minutes if body else 5
    result = await deps.alarm_scheduler.snooze(alarm_id, minutes)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


# ---------------------------------------------------------------------------
# Holidays
# ---------------------------------------------------------------------------


@router.get("/holidays/{year}")
async def get_holidays_list(year: int, country: str = "FR"):
    """List public holidays for a year."""
    from tune_server.alarms import get_holidays
    holidays = get_holidays(year, country)
    return [{"date": d.isoformat(), "weekday": d.strftime("%A")} for d in sorted(holidays)]


# ---------------------------------------------------------------------------
# Legacy /alarms (plural) endpoints — delegate to the same logic
# ---------------------------------------------------------------------------


@legacy_router.get("/", response_model=list[AlarmResponse])
async def list_alarms_legacy():
    return await list_alarms()


@legacy_router.post("/", response_model=AlarmResponse)
async def create_alarm_legacy(body: AlarmCreate):
    return await create_alarm(body)


@legacy_router.put("/{alarm_id}", response_model=AlarmResponse)
async def update_alarm_legacy(alarm_id: int, body: AlarmUpdate):
    return await update_alarm(alarm_id, body)


@legacy_router.delete("/{alarm_id}")
async def delete_alarm_legacy(alarm_id: int):
    return await delete_alarm(alarm_id)


@legacy_router.get("/holidays/{year}")
async def get_holidays_legacy(year: int, country: str = "FR"):
    return await get_holidays_list(year, country)

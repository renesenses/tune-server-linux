"""Alarm management API routes."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from tune_server.api.deps import deps as _deps

router = APIRouter(prefix="/alarms", tags=["alarms"])


@router.get("/")
async def list_alarms():
    deps = _deps
    rows = await deps.db.fetchall("SELECT * FROM alarms ORDER BY time")
    return [dict(r) for r in rows]


@router.post("/")
async def create_alarm(body: dict):
    deps = _deps
    await deps.db.execute(
        "INSERT INTO alarms (name, time, days, skip_holidays, holiday_country, "
        "zone_id, source_type, source_id, source_name, volume, fade_in_seconds, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            body.get("name", "Alarm"),
            body["time"],
            body.get("days", "1,2,3,4,5"),
            1 if body.get("skip_holidays") else 0,
            body.get("holiday_country", "FR"),
            body.get("zone_id"),
            body.get("source_type", "radio"),
            body.get("source_id", ""),
            body.get("source_name"),
            body.get("volume", 50),
            body.get("fade_in_seconds", 30),
            1 if body.get("enabled", True) else 0,
        ),
    )
    await deps.db.commit()
    row = await deps.db.fetchone("SELECT * FROM alarms ORDER BY id DESC LIMIT 1")
    return dict(row)


@router.put("/{alarm_id}")
async def update_alarm(alarm_id: int, body: dict):
    deps = _deps
    sets = []
    vals = []
    for key in ("name", "time", "days", "skip_holidays", "holiday_country",
                "zone_id", "source_type", "source_id", "source_name",
                "volume", "fade_in_seconds", "enabled"):
        if key in body:
            val = body[key]
            if key in ("skip_holidays", "enabled"):
                val = 1 if val else 0
            sets.append(f"{key} = ?")
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
    return dict(row) if row else {"error": "not found"}


@router.delete("/{alarm_id}")
async def delete_alarm(alarm_id: int):
    deps = _deps
    await deps.db.execute("DELETE FROM alarms WHERE id = ?", (alarm_id,))
    await deps.db.commit()
    return {"deleted": alarm_id}


@router.get("/holidays/{year}")
async def get_holidays_list(year: int, country: str = "FR"):
    from tune_server.alarms import get_holidays
    holidays = get_holidays(year, country)
    return [{"date": d.isoformat(), "weekday": d.strftime("%A")} for d in sorted(holidays)]

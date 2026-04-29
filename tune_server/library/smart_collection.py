"""Smart Collections — auto-rule-based album collections (v0.8.0 POC).

A Smart Collection stores rules over the `albums` table (plus a few
cross-table operators like ``credit.has`` that join through
`tracks` → `track_credits`). Membership is computed lazily per GET
— the rules are compiled to a SQL ``WHERE`` clause and the resulting
album rows are returned. We don't materialise membership into a
separate table; libraries up to ~10 k albums query cheaply, and
materialisation would just be a stale-cache problem.

Rule shape (JSON list, stored in `smart_collections.rules`)::

    [
        {"field": "sample_rate", "op": ">=", "value": 96000},
        {"field": "label", "op": "contains", "value": "Blue Note"},
        {"field": "credit", "op": "has",
         "value": {"role": "engineer", "artist_name": "Rudy Van Gelder"}}
    ]

The wrapper `match_mode` (``'all'`` or ``'any'``) glues them with
AND or OR.

Supported operators per field type:

  - text fields (label, genre, artist_name, format, source, title):
    ``=``, ``!=``, ``contains``, ``starts_with``, ``in``,
    ``is_null``, ``is_not_null``
  - int fields (year, sample_rate, bit_depth, track_count):
    ``=``, ``!=``, ``>``, ``>=``, ``<``, ``<=``, ``between``, ``in``
  - timestamp fields (added_at, last_played_at):
    ``>``, ``<``, ``between``, ``is_null`` (with relative values like
    ``"now-30d"``)
  - cross-table (credit, play_count):
    ``has``, ``>=``, ``<``, ``between``

Field whitelist is enforced — anything outside it raises ValueError
so a stray rule from a malicious / buggy client can't inject
arbitrary SQL columns.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Field whitelist — keys are the rule.field names, values describe their
# DB origin so the compiler can build the right SELECT/JOIN/WHERE.
# ---------------------------------------------------------------------------

_ALBUM_FIELDS_TEXT = {
    "title", "artist_name", "label", "genre", "format", "source",
    "catalog_number",
}
_ALBUM_FIELDS_INT = {
    "year", "sample_rate", "bit_depth", "track_count", "disc_count",
}
_ALBUM_FIELDS_NULLABLE = {
    "cover_path", "musicbrainz_release_id",
}
_ALBUM_FIELDS_TIMESTAMP = {
    "added_at", "created_at", "updated_at",
}
# Aliases to map user-facing names to DB column names.
_ALBUM_FIELD_ALIAS = {
    "added_at": "created_at",  # `albums.created_at` IS the "added at" timestamp
}

# Cross-table fields that require a subquery rather than a direct
# `albums.column` filter. Each entry's value is the SQL builder lambda
# applied to the canonical (op, value) tuple.
_CROSS_TABLE_FIELDS = {"credit", "play_count", "last_played_at"}

_TEXT_OPS = {"=", "!=", "contains", "starts_with", "in", "is_null", "is_not_null"}
_INT_OPS = {"=", "!=", ">", ">=", "<", "<=", "between", "in", "is_null", "is_not_null"}
_TIMESTAMP_OPS = {">", ">=", "<", "<=", "between", "is_null", "is_not_null"}


# ---------------------------------------------------------------------------
# Compiler: rule → SQL fragment
# ---------------------------------------------------------------------------


def _resolve_relative_timestamp(value: Any) -> Any:
    """Translate ``"now-30d"`` / ``"now-12h"`` / ``"now"`` to an ISO datetime
    string. Absolute values pass through unchanged."""
    if not isinstance(value, str) or not value.startswith("now"):
        return value
    rest = value[3:].strip()
    if not rest:
        return datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat()
    sign = 1 if rest.startswith("+") else -1
    rest = rest.lstrip("+-")
    if rest.endswith("d"):
        delta = timedelta(days=int(rest[:-1]))
    elif rest.endswith("h"):
        delta = timedelta(hours=int(rest[:-1]))
    elif rest.endswith("m"):
        delta = timedelta(minutes=int(rest[:-1]))
    elif rest.endswith("y"):
        delta = timedelta(days=365 * int(rest[:-1]))
    else:
        raise ValueError(f"unknown relative timestamp suffix in {value!r}")
    return (datetime.now(tz=timezone.utc).replace(tzinfo=None) - sign * (-delta)).isoformat()


def _compile_text_rule(field: str, op: str, value: Any) -> tuple[str, list]:
    if op not in _TEXT_OPS:
        raise ValueError(f"unsupported op {op!r} for text field {field!r}")
    col = f"albums.{_ALBUM_FIELD_ALIAS.get(field, field)}"
    if op == "=":
        return f"{col} = ?", [value]
    if op == "!=":
        return f"({col} IS NULL OR {col} != ?)", [value]
    if op == "contains":
        return f"{col} LIKE ?", [f"%{value}%"]
    if op == "starts_with":
        return f"{col} LIKE ?", [f"{value}%"]
    if op == "in":
        if not isinstance(value, list) or not value:
            return "1=0", []
        placeholders = ",".join(["?"] * len(value))
        return f"{col} IN ({placeholders})", list(value)
    if op == "is_null":
        return f"{col} IS NULL", []
    if op == "is_not_null":
        return f"{col} IS NOT NULL", []
    raise ValueError(f"unhandled text op {op!r}")  # pragma: no cover


def _compile_int_rule(field: str, op: str, value: Any) -> tuple[str, list]:
    if op not in _INT_OPS:
        raise ValueError(f"unsupported op {op!r} for int field {field!r}")
    col = f"albums.{_ALBUM_FIELD_ALIAS.get(field, field)}"
    if op == "is_null":
        return f"{col} IS NULL", []
    if op == "is_not_null":
        return f"{col} IS NOT NULL", []
    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(f"`between` expects [min, max], got {value!r}")
        lo, hi = value
        return f"{col} BETWEEN ? AND ?", [lo, hi]
    if op == "in":
        if not isinstance(value, list) or not value:
            return "1=0", []
        placeholders = ",".join(["?"] * len(value))
        return f"{col} IN ({placeholders})", list(value)
    return f"{col} {op} ?", [value]


def _compile_timestamp_rule(field: str, op: str, value: Any) -> tuple[str, list]:
    if op not in _TIMESTAMP_OPS:
        raise ValueError(f"unsupported op {op!r} for timestamp field {field!r}")
    col = f"albums.{_ALBUM_FIELD_ALIAS.get(field, field)}"
    if op == "is_null":
        return f"{col} IS NULL", []
    if op == "is_not_null":
        return f"{col} IS NOT NULL", []
    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(f"`between` expects [start, end], got {value!r}")
        a, b = _resolve_relative_timestamp(value[0]), _resolve_relative_timestamp(value[1])
        return f"{col} BETWEEN ? AND ?", [a, b]
    return f"{col} {op} ?", [_resolve_relative_timestamp(value)]


def _compile_cross_table_rule(field: str, op: str, value: Any) -> tuple[str, list]:
    """Subquery-based rules: credit.has, play_count, last_played_at."""
    if field == "credit":
        if op != "has":
            raise ValueError(f"unsupported op {op!r} for field 'credit' (only 'has')")
        if not isinstance(value, dict):
            raise ValueError("`credit.has` expects a dict {role?, artist_name?, instrument?}")
        clauses: list[str] = []
        params: list = []
        for k in ("role", "artist_name", "instrument"):
            if k in value:
                clauses.append(f"track_credits.{k} = ?")
                params.append(value[k])
        if not clauses:
            raise ValueError("`credit.has` requires at least one of role/artist_name/instrument")
        where = " AND ".join(clauses)
        sql = (
            "albums.id IN ("
            "SELECT t.album_id FROM tracks t "
            "JOIN track_credits ON track_credits.track_id = t.id "
            f"WHERE {where} AND t.album_id IS NOT NULL"
            ")"
        )
        return sql, params
    if field == "play_count":
        if op == "is_null":
            return (
                "albums.id NOT IN ("
                "SELECT t.album_id FROM tracks t "
                "JOIN playback_history ph ON ph.track_id = t.id "
                "WHERE t.album_id IS NOT NULL)"
            ), []
        if op not in {">=", ">", "=", "<=", "<", "between"}:
            raise ValueError(f"unsupported op {op!r} for field 'play_count'")
        having_op_sql, having_params = _compile_count_having("play_count", op, value)
        sql = (
            "albums.id IN ("
            "SELECT t.album_id FROM tracks t "
            "JOIN playback_history ph ON ph.track_id = t.id "
            "WHERE t.album_id IS NOT NULL "
            "GROUP BY t.album_id "
            f"HAVING {having_op_sql})"
        )
        return sql, having_params
    if field == "last_played_at":
        if op == "is_null":
            return (
                "albums.id NOT IN ("
                "SELECT t.album_id FROM tracks t "
                "JOIN playback_history ph ON ph.track_id = t.id "
                "WHERE t.album_id IS NOT NULL)"
            ), []
        if op not in {">", ">=", "<", "<=", "between"}:
            raise ValueError(f"unsupported op {op!r} for field 'last_played_at'")
        if op == "between":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise ValueError("`between` expects [start, end]")
            a = _resolve_relative_timestamp(value[0])
            b = _resolve_relative_timestamp(value[1])
            having = "MAX(ph.played_at) BETWEEN ? AND ?"
            params = [a, b]
        else:
            having = f"MAX(ph.played_at) {op} ?"
            params = [_resolve_relative_timestamp(value)]
        sql = (
            "albums.id IN ("
            "SELECT t.album_id FROM tracks t "
            "JOIN playback_history ph ON ph.track_id = t.id "
            "WHERE t.album_id IS NOT NULL "
            f"GROUP BY t.album_id HAVING {having})"
        )
        return sql, params
    raise ValueError(f"unknown cross-table field {field!r}")


def _compile_count_having(name: str, op: str, value: Any) -> tuple[str, list]:
    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(f"`{name}.between` expects [min, max]")
        return f"COUNT(*) BETWEEN ? AND ?", [value[0], value[1]]
    return f"COUNT(*) {op} ?", [value]


def compile_rule(rule: dict) -> tuple[str, list]:
    field = rule.get("field")
    op = rule.get("op")
    value = rule.get("value")
    if not field or not op:
        raise ValueError(f"rule missing field/op: {rule!r}")
    if field in _CROSS_TABLE_FIELDS:
        return _compile_cross_table_rule(field, op, value)
    if field in _ALBUM_FIELDS_TEXT or field in _ALBUM_FIELDS_NULLABLE:
        return _compile_text_rule(field, op, value)
    if field in _ALBUM_FIELDS_INT:
        return _compile_int_rule(field, op, value)
    if field in _ALBUM_FIELDS_TIMESTAMP:
        return _compile_timestamp_rule(field, op, value)
    raise ValueError(
        f"unknown field {field!r} (allowed: "
        f"{sorted(_ALBUM_FIELDS_TEXT | _ALBUM_FIELDS_INT | _ALBUM_FIELDS_NULLABLE | _ALBUM_FIELDS_TIMESTAMP | _CROSS_TABLE_FIELDS)})"
    )


_VALID_SORT_FIELDS = {
    "added_at", "created_at", "title", "artist_name", "year", "label",
    "sample_rate",
}
_VALID_SORT_ORDERS = {"asc", "desc"}


def compile_rules(
    rules: list[dict],
    match_mode: str = "all",
    sort_by: str = "added_at",
    sort_order: str = "desc",
    max_albums: int = 500,
) -> tuple[str, list]:
    """Build the full SELECT for a Smart Collection's membership."""
    if match_mode not in ("all", "any"):
        match_mode = "all"
    if sort_by not in _VALID_SORT_FIELDS:
        sort_by = "added_at"
    if sort_order not in _VALID_SORT_ORDERS:
        sort_order = "desc"
    sort_col = _ALBUM_FIELD_ALIAS.get(sort_by, sort_by)
    where_parts: list[str] = []
    params: list = []
    for r in rules:
        sql, p = compile_rule(r)
        where_parts.append(f"({sql})")
        params.extend(p)
    glue = " AND " if match_mode == "all" else " OR "
    where_sql = glue.join(where_parts) if where_parts else "1=1"
    final = (
        f"SELECT albums.* FROM albums "
        f"WHERE {where_sql} "
        f"ORDER BY albums.{sort_col} {sort_order.upper()} "
        f"LIMIT ?"
    )
    return final, params + [max(1, min(int(max_albums), 5000))]


# ---------------------------------------------------------------------------
# Repo + cache
# ---------------------------------------------------------------------------


_CACHE: dict[int, tuple[float, list[dict]]] = {}
_CACHE_TTL_S = 30


def invalidate_cache(collection_id: Optional[int] = None) -> None:
    if collection_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(collection_id, None)


class SmartCollectionRepo:
    """Thin CRUD over the `smart_collections` table + lazy membership
    compute. Sits on top of the existing legacy `Database` engine so
    the same code path works on SQLite and PostgreSQL."""

    def __init__(self, db) -> None:
        self._db = db

    async def list(self) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM smart_collections ORDER BY name"
        )
        return [dict(r) for r in rows]

    async def get(self, collection_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM smart_collections WHERE id = ?", (collection_id,)
        )
        return dict(row) if row else None

    async def create(self, payload: dict) -> int:
        rules_json = json.dumps(payload.get("rules", []))
        result = await self._db.execute(
            """INSERT INTO smart_collections
                 (name, description, icon, color, rules, match_mode,
                  sort_by, sort_order, max_albums, auto_refresh)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (
                payload.get("name", "Smart Collection"),
                payload.get("description"),
                payload.get("icon", "folder"),
                payload.get("color", "#6366f1"),
                rules_json,
                payload.get("match_mode", "all"),
                payload.get("sort_by", "added_at"),
                payload.get("sort_order", "desc"),
                payload.get("max_albums", 500),
                1 if payload.get("auto_refresh", True) else 0,
            ),
        )
        await self._db.commit()
        return result.lastrowid

    async def update(self, collection_id: int, payload: dict) -> None:
        fields = []
        values: list = []
        for col in (
            "name", "description", "icon", "color", "match_mode",
            "sort_by", "sort_order", "max_albums",
        ):
            if col in payload:
                fields.append(f"{col} = ?")
                values.append(payload[col])
        if "rules" in payload:
            fields.append("rules = ?")
            values.append(json.dumps(payload["rules"]))
        if "auto_refresh" in payload:
            fields.append("auto_refresh = ?")
            values.append(1 if payload["auto_refresh"] else 0)
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(collection_id)
        await self._db.execute(
            f"UPDATE smart_collections SET {', '.join(fields)} WHERE id = ?",
            tuple(values),
        )
        await self._db.commit()
        invalidate_cache(collection_id)

    async def delete(self, collection_id: int) -> None:
        await self._db.execute(
            "DELETE FROM smart_collections WHERE id = ?", (collection_id,)
        )
        await self._db.commit()
        invalidate_cache(collection_id)

    async def evaluate(self, collection_id: int) -> list[dict]:
        """Compute the album set for this Smart Collection. Cached 30 s
        in-process — invalidated by `invalidate_cache()` on
        scan_completed / track_completed events."""
        cached = _CACHE.get(collection_id)
        now = time.monotonic()
        if cached and now - cached[0] < _CACHE_TTL_S:
            return cached[1]
        record = await self.get(collection_id)
        if record is None:
            return []
        try:
            rules = json.loads(record["rules"]) if record["rules"] else []
        except json.JSONDecodeError:
            rules = []
        sql, params = compile_rules(
            rules,
            match_mode=record.get("match_mode") or "all",
            sort_by=record.get("sort_by") or "added_at",
            sort_order=record.get("sort_order") or "desc",
            max_albums=record.get("max_albums") or 500,
        )
        try:
            rows = await self._db.fetchall(sql, tuple(params))
        except Exception as exc:
            logger.warning(
                "smart_collection_eval_failed",
                collection_id=collection_id, error=repr(exc),
            )
            return []
        albums = [dict(r) for r in rows]
        _CACHE[collection_id] = (now, albums)
        return albums


# ---------------------------------------------------------------------------
# Default smart collections — seeded on first server start so users get
# value out of the box without having to author rules from scratch.
# ---------------------------------------------------------------------------

DEFAULT_SMART_COLLECTIONS: list[dict] = [
    {
        "name": "Hi-Res",
        "description": "Albums sample rate ≥ 96 kHz",
        "icon": "waveform.path.ecg",
        "color": "#9333ea",
        "rules": [{"field": "sample_rate", "op": ">=", "value": 96000}],
    },
    {
        "name": "DSD",
        "description": "Albums au format DSD natif",
        "icon": "music.note",
        "color": "#ec4899",
        "rules": [{"field": "format", "op": "=", "value": "dsd"}],
    },
    {
        "name": "Récents (30 j)",
        "description": "Albums ajoutés dans les 30 derniers jours",
        "icon": "clock",
        "color": "#10b981",
        "rules": [{"field": "added_at", "op": ">", "value": "now-30d"}],
        "sort_by": "added_at",
        "sort_order": "desc",
    },
    {
        "name": "Sans pochette",
        "description": "Albums dont la couverture est manquante",
        "icon": "photo",
        "color": "#f59e0b",
        "rules": [{"field": "cover_path", "op": "is_null", "value": None}],
    },
    {
        "name": "Jamais écoutés",
        "description": "Albums présents en bibliothèque mais jamais joués",
        "icon": "questionmark.circle",
        "color": "#6b7280",
        "rules": [{"field": "last_played_at", "op": "is_null", "value": None}],
    },
    {
        "name": "Heavy rotation",
        "description": "Albums avec ≥ 10 lectures cumulées",
        "icon": "repeat",
        "color": "#dc2626",
        "rules": [{"field": "play_count", "op": ">=", "value": 10}],
    },
    {
        "name": "Albums Blue Note",
        "description": "Albums du label Blue Note (jazz)",
        "icon": "tag",
        "color": "#0ea5e9",
        "rules": [{"field": "label", "op": "contains", "value": "Blue Note"}],
    },
]


async def seed_default_collections(repo: SmartCollectionRepo) -> int:
    """Seed default smart collections on first server start. Idempotent —
    skip names that already exist (allows users to delete defaults
    they don't want without them coming back)."""
    existing = {row["name"] for row in await repo.list()}
    inserted = 0
    for spec in DEFAULT_SMART_COLLECTIONS:
        if spec["name"] in existing:
            continue
        try:
            await repo.create(spec)
            inserted += 1
        except Exception:
            logger.exception("smart_collection_seed_failed", name=spec["name"])
    return inserted

"""Tests for Smart Collections rules engine + repo (v0.8.0).

Focuses on the rules → SQL compiler. Repo CRUD is exercised end-to-end
via an in-memory SQLite database with a minimal schema.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tune_server.library.smart_collection import (
    DEFAULT_SMART_COLLECTIONS,
    SmartCollectionRepo,
    _resolve_relative_timestamp,
    compile_rule,
    compile_rules,
    invalidate_cache,
)


# ---------------------------------------------------------------------------
# Compiler — single-rule unit tests
# ---------------------------------------------------------------------------


class TestTextRules:
    def test_equals(self):
        sql, params = compile_rule({"field": "label", "op": "=", "value": "Blue Note"})
        assert "albums.label = ?" in sql
        assert params == ["Blue Note"]

    def test_contains(self):
        sql, params = compile_rule(
            {"field": "genre", "op": "contains", "value": "jazz"}
        )
        assert "albums.genre LIKE ?" in sql
        assert params == ["%jazz%"]

    def test_starts_with(self):
        sql, params = compile_rule(
            {"field": "artist_name", "op": "starts_with", "value": "Pink"}
        )
        assert "LIKE ?" in sql
        assert params == ["Pink%"]

    def test_in_list(self):
        sql, params = compile_rule(
            {"field": "format", "op": "in", "value": ["flac", "wav"]}
        )
        assert "albums.format IN (?,?)" in sql
        assert params == ["flac", "wav"]

    def test_in_empty_yields_unsat(self):
        sql, params = compile_rule({"field": "format", "op": "in", "value": []})
        assert sql == "1=0"
        assert params == []

    def test_is_null(self):
        sql, params = compile_rule(
            {"field": "cover_path", "op": "is_null", "value": None}
        )
        assert "IS NULL" in sql
        assert params == []

    def test_not_equals_handles_null(self):
        sql, _ = compile_rule({"field": "label", "op": "!=", "value": "Pop"})
        # Albums with NULL label should still match `!= 'Pop'`.
        assert "IS NULL" in sql

    def test_unsupported_op_raises(self):
        with pytest.raises(ValueError, match="unsupported op"):
            compile_rule({"field": "label", "op": "regex", "value": "x"})


class TestIntRules:
    def test_gte(self):
        sql, params = compile_rule(
            {"field": "sample_rate", "op": ">=", "value": 96000}
        )
        assert "albums.sample_rate >= ?" in sql
        assert params == [96000]

    def test_between(self):
        sql, params = compile_rule(
            {"field": "year", "op": "between", "value": [1970, 1979]}
        )
        assert "BETWEEN ? AND ?" in sql
        assert params == [1970, 1979]

    def test_between_bad_value_raises(self):
        with pytest.raises(ValueError, match="`between` expects"):
            compile_rule({"field": "year", "op": "between", "value": 1970})


class TestTimestampRules:
    def test_relative_now_minus_30d(self):
        v = _resolve_relative_timestamp("now-30d")
        assert isinstance(v, str) and "T" in v  # ISO datetime

    def test_relative_now(self):
        v = _resolve_relative_timestamp("now")
        assert isinstance(v, str)

    def test_absolute_passthrough(self):
        v = _resolve_relative_timestamp("2024-01-01T00:00:00")
        assert v == "2024-01-01T00:00:00"

    def test_unknown_suffix_raises(self):
        with pytest.raises(ValueError):
            _resolve_relative_timestamp("now-30z")

    def test_added_at_alias(self):
        # `added_at` should map to `albums.created_at`.
        sql, _ = compile_rule(
            {"field": "added_at", "op": ">", "value": "now-30d"}
        )
        assert "albums.created_at >" in sql


class TestCrossTableRules:
    def test_credit_has_full_triple(self):
        sql, params = compile_rule({
            "field": "credit", "op": "has",
            "value": {"role": "engineer", "artist_name": "Rudy Van Gelder"},
        })
        assert "track_credits" in sql
        assert "tracks" in sql
        assert "track_credits.role = ?" in sql
        assert "track_credits.artist_name = ?" in sql
        assert "Rudy Van Gelder" in params
        assert "engineer" in params

    def test_credit_has_artist_only(self):
        sql, params = compile_rule({
            "field": "credit", "op": "has",
            "value": {"artist_name": "Herbie Hancock"},
        })
        # Single criterion — no role/instrument condition emitted.
        assert "track_credits.role" not in sql
        assert params == ["Herbie Hancock"]

    def test_credit_requires_at_least_one_criterion(self):
        with pytest.raises(ValueError, match="at least one"):
            compile_rule({"field": "credit", "op": "has", "value": {}})

    def test_credit_unsupported_op(self):
        with pytest.raises(ValueError, match="only 'has'"):
            compile_rule({"field": "credit", "op": "=", "value": {}})

    def test_play_count_gte(self):
        sql, params = compile_rule(
            {"field": "play_count", "op": ">=", "value": 10}
        )
        assert "playback_history" in sql
        assert "GROUP BY t.album_id" in sql
        assert "HAVING COUNT(*) >= ?" in sql
        assert params == [10]

    def test_last_played_at_is_null(self):
        sql, params = compile_rule(
            {"field": "last_played_at", "op": "is_null", "value": None}
        )
        assert "albums.id NOT IN" in sql
        assert params == []


class TestUnknownField:
    def test_raises(self):
        with pytest.raises(ValueError, match="unknown field"):
            compile_rule({"field": "totally_made_up", "op": "=", "value": "x"})


# ---------------------------------------------------------------------------
# Compiler — full SELECT
# ---------------------------------------------------------------------------


class TestCompileRules:
    def test_match_all_glues_with_AND(self):
        sql, params = compile_rules(
            [
                {"field": "label", "op": "=", "value": "Blue Note"},
                {"field": "year", "op": "between", "value": [1955, 1970]},
            ],
            match_mode="all",
        )
        assert " AND " in sql
        assert " OR " not in sql
        assert params[:3] == ["Blue Note", 1955, 1970]
        assert "ORDER BY albums.created_at DESC" in sql  # added_at default
        assert "LIMIT ?" in sql

    def test_match_any_glues_with_OR(self):
        sql, _ = compile_rules(
            [
                {"field": "label", "op": "=", "value": "Blue Note"},
                {"field": "label", "op": "=", "value": "ECM"},
            ],
            match_mode="any",
        )
        assert " OR " in sql

    def test_invalid_sort_field_falls_back_to_added_at(self):
        sql, _ = compile_rules([], sort_by="injected; DROP TABLE--")
        assert "albums.created_at" in sql

    def test_invalid_sort_order_falls_back_to_desc(self):
        sql, _ = compile_rules([], sort_order="DROP")
        assert "DESC" in sql

    def test_max_albums_clamped(self):
        _, params = compile_rules([], max_albums=999_999)
        assert params[-1] == 5000  # capped

    def test_max_albums_min(self):
        _, params = compile_rules([], max_albums=0)
        assert params[-1] == 1

    def test_empty_rules_yields_match_all(self):
        sql, _ = compile_rules([])
        assert "WHERE 1=1" in sql


# ---------------------------------------------------------------------------
# Default collections — ensure each one compiles without error
# ---------------------------------------------------------------------------


class TestDefaultCollections:
    def test_each_default_compiles(self):
        for spec in DEFAULT_SMART_COLLECTIONS:
            sql, params = compile_rules(
                spec["rules"],
                match_mode=spec.get("match_mode", "all"),
                sort_by=spec.get("sort_by", "added_at"),
                sort_order=spec.get("sort_order", "desc"),
                max_albums=spec.get("max_albums", 500),
            )
            assert sql.startswith("SELECT albums.*, COALESCE(NULLIF(albums.artist_name, ''), artists.name)")
            assert "LEFT JOIN artists" in sql
            assert isinstance(params, list)


# ---------------------------------------------------------------------------
# SmartCollectionRepo — end-to-end on in-memory SQLite
# ---------------------------------------------------------------------------


import aiosqlite


class _FakeDB:
    """Minimal Database adapter mirroring the methods the repo uses
    (execute / fetchone / fetchall / commit). aiosqlite under the hood.

    Cursors are explicitly closed before commit() to avoid "SQL
    statements in progress" lock contention — matches the behaviour
    of tune_server.db.engine.Database.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: tuple = ()):  # noqa: ANN001
        cur = await self._conn.execute(sql, params)
        try:
            lastrowid = cur.lastrowid
            # RETURNING clauses must be consumed before commit, same as
            # the real Database adapter (db/engine.py).
            if "RETURNING" in sql.upper():
                row = await cur.fetchone()
                if row and lastrowid is None:
                    lastrowid = row[0]
        finally:
            await cur.close()
        return _Result(lastrowid)

    async def fetchone(self, sql: str, params: tuple = ()):  # noqa: ANN001
        cur = await self._conn.execute(sql, params)
        try:
            row = await cur.fetchone()
            if row is None:
                return None
            cols = [c[0] for c in cur.description]
            return _Row(row, cols)
        finally:
            await cur.close()

    async def fetchall(self, sql: str, params: tuple = ()):  # noqa: ANN001
        cur = await self._conn.execute(sql, params)
        try:
            rows = await cur.fetchall()
            cols = [c[0] for c in cur.description]
            return [_Row(r, cols) for r in rows]
        finally:
            await cur.close()

    async def commit(self) -> None:
        await self._conn.commit()


class _Result:
    """Tiny stand-in for ExecuteResult — only exposes the bits used."""

    def __init__(self, lastrowid: int | None) -> None:
        self.lastrowid = lastrowid


class _Row:
    """Mimics aiosqlite.Row's dict-like access."""

    def __init__(self, row, cols) -> None:  # noqa: ANN001
        self._row = row
        self._cols = cols

    def __getitem__(self, key):  # noqa: ANN001
        if isinstance(key, int):
            return self._row[key]
        return self._row[self._cols.index(key)]

    def get(self, key, default=None):  # noqa: ANN001
        try:
            return self[key]
        except (KeyError, ValueError):
            return default

    def keys(self):
        return list(self._cols)


@pytest.fixture
async def smart_repo():
    invalidate_cache()
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("""
        CREATE TABLE smart_collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            icon TEXT DEFAULT 'folder',
            color TEXT DEFAULT '#6366f1',
            rules TEXT NOT NULL,
            match_mode TEXT DEFAULT 'all',
            sort_by TEXT DEFAULT 'added_at',
            sort_order TEXT DEFAULT 'desc',
            max_albums INTEGER DEFAULT 500,
            auto_refresh INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await conn.execute("""
        CREATE TABLE albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            artist_id INTEGER,
            artist_name TEXT,
            year INTEGER,
            genre TEXT,
            label TEXT,
            format TEXT,
            sample_rate INTEGER,
            bit_depth INTEGER,
            cover_path TEXT,
            track_count INTEGER DEFAULT 0,
            disc_count INTEGER DEFAULT 1,
            source TEXT DEFAULT 'local',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            musicbrainz_release_id TEXT,
            catalog_number TEXT
        )
    """)
    await conn.execute("""
        CREATE TABLE artists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        )
    """)
    await conn.commit()
    repo = SmartCollectionRepo(_FakeDB(conn))
    yield repo
    await conn.close()


@pytest.mark.asyncio
async def test_repo_create_get_delete_roundtrip(smart_repo):
    sc_id = await smart_repo.create({
        "name": "Hi-Res", "rules": [
            {"field": "sample_rate", "op": ">=", "value": 96000},
        ],
    })
    rec = await smart_repo.get(sc_id)
    assert rec is not None
    assert rec["name"] == "Hi-Res"
    assert "sample_rate" in rec["rules"]

    await smart_repo.delete(sc_id)
    assert await smart_repo.get(sc_id) is None


@pytest.mark.asyncio
async def test_repo_evaluate_filters_albums(smart_repo):
    # Seed 3 albums, only 2 are Hi-Res.
    db = smart_repo._db._conn  # type: ignore[attr-defined]
    await db.execute(
        "INSERT INTO albums (title, sample_rate, label) VALUES (?, ?, ?)",
        ("Kind of Blue", 96000, "Columbia"),
    )
    await db.execute(
        "INSERT INTO albums (title, sample_rate, label) VALUES (?, ?, ?)",
        ("CD Album", 44100, "Generic"),
    )
    await db.execute(
        "INSERT INTO albums (title, sample_rate, label) VALUES (?, ?, ?)",
        ("DSD Album", 192000, "Blue Note"),
    )
    await db.commit()

    sc_id = await smart_repo.create({
        "name": "Hi-Res", "rules": [
            {"field": "sample_rate", "op": ">=", "value": 96000},
        ],
    })
    albums = await smart_repo.evaluate(sc_id)
    titles = sorted(a["title"] for a in albums)
    assert titles == ["DSD Album", "Kind of Blue"]


@pytest.mark.asyncio
async def test_repo_evaluate_label_contains(smart_repo):
    db = smart_repo._db._conn  # type: ignore[attr-defined]
    for title, label in [
        ("Kind of Blue", "Columbia"),
        ("Maiden Voyage", "Blue Note"),
        ("Speak No Evil", "Blue Note Records"),
    ]:
        await db.execute(
            "INSERT INTO albums (title, label) VALUES (?, ?)", (title, label),
        )
    await db.commit()

    sc_id = await smart_repo.create({
        "name": "Blue Note family",
        "rules": [{"field": "label", "op": "contains", "value": "Blue Note"}],
    })
    albums = await smart_repo.evaluate(sc_id)
    titles = sorted(a["title"] for a in albums)
    assert titles == ["Maiden Voyage", "Speak No Evil"]


@pytest.mark.asyncio
async def test_repo_update_invalidates_cache(smart_repo):
    db = smart_repo._db._conn  # type: ignore[attr-defined]
    await db.execute("INSERT INTO albums (title, year) VALUES (?, ?)", ("70s", 1975))
    await db.execute("INSERT INTO albums (title, year) VALUES (?, ?)", ("80s", 1985))
    await db.commit()

    sc_id = await smart_repo.create({
        "name": "70s", "rules": [{"field": "year", "op": "between", "value": [1970, 1979]}],
    })
    titles_v1 = sorted(a["title"] for a in await smart_repo.evaluate(sc_id))
    assert titles_v1 == ["70s"]

    await smart_repo.update(sc_id, {
        "rules": [{"field": "year", "op": "between", "value": [1980, 1989]}],
    })
    titles_v2 = sorted(a["title"] for a in await smart_repo.evaluate(sc_id))
    assert titles_v2 == ["80s"]


@pytest.mark.asyncio
async def test_repo_evaluate_handles_invalid_rules_gracefully(smart_repo):
    # Insert a record with a malformed JSON rules list to simulate
    # data corruption / migration glitch.
    db = smart_repo._db._conn  # type: ignore[attr-defined]
    await db.execute(
        "INSERT INTO smart_collections (name, rules) VALUES (?, ?)",
        ("broken", "{not-json"),
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM smart_collections WHERE name='broken'")
    row = await cur.fetchone()
    sc_id = row[0]
    # Should not raise — invalid JSON falls back to an empty rules list
    # which means "match everything".
    albums = await smart_repo.evaluate(sc_id)
    assert isinstance(albums, list)

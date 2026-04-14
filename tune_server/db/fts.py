"""Full-Text Search plugin system — engine-specific FTS abstraction.

Each database engine uses different FTS mechanisms:
- SQLite: FTS5 virtual tables with MATCH syntax
- PostgreSQL: tsvector columns + GIN indexes + @@ operator
- Generic: LIKE/ILIKE fallback for MySQL, MariaDB, etc.

The plugin is injected into SADatabase.fts at connection time.
"""
from __future__ import annotations

from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

import structlog

logger = structlog.get_logger()


class FTSPlugin(Protocol):
    """Abstract FTS interface — implemented per engine."""

    async def setup(self, conn: AsyncConnection) -> None:
        """Create FTS structures (virtual tables, indexes, triggers)."""
        ...

    def search_where(self, table_name: str, query: str) -> sa.TextClause:
        """Return a WHERE clause for full-text matching."""
        ...

    def search_rank(self, table_name: str, query: str) -> sa.TextClause:
        """Return an ORDER BY expression for relevance ranking."""
        ...


class SQLiteFTS:
    """FTS5 virtual tables with triggers for auto-sync."""

    _FTS_TABLES = {
        "tracks": "title",
        "albums": "title",
        "artists": "name",
    }

    async def setup(self, conn: AsyncConnection) -> None:
        """Create FTS5 virtual tables and sync triggers."""
        for table, column in self._FTS_TABLES.items():
            fts_name = f"{table}_fts"
            stmts = [
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts_name} USING fts5({column}, content={table}, content_rowid=id, tokenize='unicode61 remove_diacritics 2')",
                # INSERT trigger
                f"""CREATE TRIGGER IF NOT EXISTS {table}_ai AFTER INSERT ON {table} BEGIN
                    INSERT INTO {fts_name}(rowid, {column}) VALUES (new.id, new.{column});
                END""",
                # DELETE trigger
                f"""CREATE TRIGGER IF NOT EXISTS {table}_ad AFTER DELETE ON {table} BEGIN
                    INSERT INTO {fts_name}({fts_name}, rowid, {column}) VALUES ('delete', old.id, old.{column});
                END""",
                # UPDATE trigger
                f"""CREATE TRIGGER IF NOT EXISTS {table}_au AFTER UPDATE OF {column} ON {table} BEGIN
                    INSERT INTO {fts_name}({fts_name}, rowid, {column}) VALUES ('delete', old.id, old.{column});
                    INSERT INTO {fts_name}(rowid, {column}) VALUES (new.id, new.{column});
                END""",
            ]
            for stmt in stmts:
                try:
                    await conn.execute(sa.text(stmt))
                except Exception:
                    pass  # Already exists

        logger.info("fts_initialized", engine="sqlite", tables=list(self._FTS_TABLES.keys()))

    def search_where(self, table_name: str, query: str) -> sa.TextClause:
        fts_name = f"{table_name}_fts"
        return sa.text(f"{table_name}.id IN (SELECT rowid FROM {fts_name} WHERE {fts_name} MATCH :fts_query)")

    def search_rank(self, table_name: str, query: str) -> sa.TextClause:
        fts_name = f"{table_name}_fts"
        return sa.text(f"(SELECT rank FROM {fts_name} WHERE {fts_name} MATCH :fts_query AND rowid = {table_name}.id)")


class PostgresFTS:
    """tsvector columns + GIN indexes with triggers."""

    _FTS_TABLES = {
        "tracks": "title",
        "albums": "title",
        "artists": "name",
    }

    async def setup(self, conn: AsyncConnection) -> None:
        """Add tsvector columns, GIN indexes, and auto-update triggers."""
        for table, column in self._FTS_TABLES.items():
            stmts = [
                # Add column (idempotent)
                f"""DO $$ BEGIN
                    ALTER TABLE {table} ADD COLUMN fts_vector tsvector;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$""",
                # GIN index
                f"CREATE INDEX IF NOT EXISTS idx_{table}_fts ON {table} USING GIN(fts_vector)",
                # Populate existing rows
                f"UPDATE {table} SET fts_vector = to_tsvector('simple', COALESCE({column}, '')) WHERE fts_vector IS NULL",
                # Trigger function
                f"""CREATE OR REPLACE FUNCTION {table}_fts_trigger() RETURNS trigger AS $$
                BEGIN
                    NEW.fts_vector := to_tsvector('simple', COALESCE(NEW.{column}, ''));
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql""",
                # Drop and recreate trigger
                f"DROP TRIGGER IF EXISTS trg_{table}_fts ON {table}",
                f"""CREATE TRIGGER trg_{table}_fts BEFORE INSERT OR UPDATE OF {column} ON {table}
                    FOR EACH ROW EXECUTE FUNCTION {table}_fts_trigger()""",
            ]
            for stmt in stmts:
                try:
                    await conn.execute(sa.text(stmt))
                except Exception:
                    pass

        logger.info("fts_initialized", engine="postgres", tables=list(self._FTS_TABLES.keys()))

    def search_where(self, table_name: str, query: str) -> sa.TextClause:
        return sa.text(f"{table_name}.fts_vector @@ plainto_tsquery('simple', :fts_query)")

    def search_rank(self, table_name: str, query: str) -> sa.TextClause:
        return sa.text(f"ts_rank({table_name}.fts_vector, plainto_tsquery('simple', :fts_query))")


class GenericFTS:
    """LIKE-based fallback for unsupported engines (MySQL, MariaDB, etc.)."""

    _FTS_COLUMNS = {
        "tracks": "title",
        "albums": "title",
        "artists": "name",
    }

    async def setup(self, conn: AsyncConnection) -> None:
        logger.info("fts_initialized", engine="generic", note="LIKE-based fallback")

    def search_where(self, table_name: str, query: str) -> sa.TextClause:
        column = self._FTS_COLUMNS.get(table_name, "title")
        return sa.text(f"LOWER({table_name}.{column}) LIKE '%' || LOWER(:fts_query) || '%'")

    def search_rank(self, table_name: str, query: str) -> sa.TextClause:
        return sa.text("1")  # No ranking for LIKE


def create_fts_plugin(engine_name: str) -> FTSPlugin:
    """Factory: select the right FTS plugin for the engine."""
    if engine_name == "sqlite":
        return SQLiteFTS()
    elif engine_name in ("postgres", "postgresql"):
        return PostgresFTS()
    else:
        return GenericFTS()

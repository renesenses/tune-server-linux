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

    async def _ensure_diacritics_tokenizer(self, conn: AsyncConnection) -> None:
        """Recreate FTS5 tables if they lack remove_diacritics 2.

        Databases created before the tokenizer option was added have FTS5
        tables that do exact character matching.  We detect this by checking
        the CREATE SQL and rebuild when necessary.
        """
        for table, column in self._FTS_TABLES.items():
            fts_name = f"{table}_fts"
            try:
                row = await conn.execute(
                    sa.text(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type='table' AND name=:fts_name"
                    ),
                    {"fts_name": fts_name},
                )
                result = row.fetchone()
                if result is None:
                    continue  # Table doesn't exist yet, will be created below
                create_sql = result[0] or ""
                if "remove_diacritics" in create_sql:
                    continue  # Already has diacritics support

                # Drop old FTS table + triggers, they will be recreated below
                logger.info("fts_rebuild_diacritics", table=fts_name)
                for stmt in [
                    f"DROP TRIGGER IF EXISTS {table}_ai",
                    f"DROP TRIGGER IF EXISTS {table}_ad",
                    f"DROP TRIGGER IF EXISTS {table}_au",
                    f"DROP TABLE IF EXISTS {fts_name}",
                ]:
                    await conn.execute(sa.text(stmt))
            except Exception as exc:
                logger.warning("fts_diacritics_check_error", table=fts_name, error=str(exc))

    async def setup(self, conn: AsyncConnection) -> None:
        """Create FTS5 virtual tables and sync triggers."""
        # Ensure existing FTS tables have remove_diacritics 2 tokenizer
        await self._ensure_diacritics_tokenizer(conn)

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

            # Rebuild FTS index to populate content (needed after table recreation)
            try:
                row = await conn.execute(
                    sa.text(f"SELECT COUNT(*) FROM {fts_name}")
                )
                fts_count = row.fetchone()[0]
                row2 = await conn.execute(
                    sa.text(f"SELECT COUNT(*) FROM {table}")
                )
                source_count = row2.fetchone()[0]
                if source_count > 0 and fts_count == 0:
                    logger.info("fts_rebuild_content", table=fts_name, rows=source_count)
                    await conn.execute(sa.text(
                        f"INSERT INTO {fts_name}({fts_name}) VALUES ('rebuild')"
                    ))
            except Exception as exc:
                logger.warning("fts_rebuild_error", table=fts_name, error=str(exc))

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
        # Enable unaccent extension for accent-insensitive search
        try:
            await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS unaccent"))
        except Exception:
            logger.warning("pg_unaccent_extension_unavailable")

        for table, column in self._FTS_TABLES.items():
            stmts = [
                # Add column (idempotent)
                f"""DO $$ BEGIN
                    ALTER TABLE {table} ADD COLUMN fts_vector tsvector;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$""",
                # GIN index
                f"CREATE INDEX IF NOT EXISTS idx_{table}_fts ON {table} USING GIN(fts_vector)",
                # Populate existing rows — use unaccent so the tsvector is accent-folded
                f"UPDATE {table} SET fts_vector = to_tsvector('simple', unaccent(COALESCE({column}, ''))) WHERE fts_vector IS NULL",
                # Trigger function — index the unaccented form
                f"""CREATE OR REPLACE FUNCTION {table}_fts_trigger() RETURNS trigger AS $$
                BEGIN
                    NEW.fts_vector := to_tsvector('simple', unaccent(COALESCE(NEW.{column}, '')));
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

        # One-time migration: re-index existing rows with unaccent.
        # Uses a marker row in a helper table to avoid repeating on every startup.
        try:
            await conn.execute(sa.text(
                "CREATE TABLE IF NOT EXISTS _fts_migrations (name TEXT PRIMARY KEY)"
            ))
            row = await conn.execute(sa.text(
                "SELECT 1 FROM _fts_migrations WHERE name = 'unaccent_reindex'"
            ))
            if row.fetchone() is None:
                for table, column in self._FTS_TABLES.items():
                    await conn.execute(sa.text(
                        f"UPDATE {table} SET fts_vector = to_tsvector('simple', unaccent(COALESCE({column}, '')))"
                    ))
                await conn.execute(sa.text(
                    "INSERT INTO _fts_migrations (name) VALUES ('unaccent_reindex')"
                ))
                logger.info("pg_fts_unaccent_reindex_complete")
        except Exception:
            pass

        logger.info("fts_initialized", engine="postgres", tables=list(self._FTS_TABLES.keys()))

    def search_where(self, table_name: str, query: str) -> sa.TextClause:
        return sa.text(f"{table_name}.fts_vector @@ plainto_tsquery('simple', unaccent(:fts_query))")

    def search_rank(self, table_name: str, query: str) -> sa.TextClause:
        return sa.text(f"ts_rank({table_name}.fts_vector, plainto_tsquery('simple', unaccent(:fts_query)))")


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

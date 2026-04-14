"""SQLAlchemy Core async engine — database-independent wrapper.

Implements DatabaseProtocol for backward compatibility with existing raw SQL
code while providing SA-native methods for new code.

Supports SQLite (aiosqlite), PostgreSQL (asyncpg), and any SA-supported async dialect.
"""
from __future__ import annotations

import re
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import structlog

from tune_server.db.engine import ExecuteResult
from tune_server.db.tables import metadata
from tune_server.db.fts import create_fts_plugin, FTSPlugin

logger = structlog.get_logger()

# Map config engine names to SA URL prefixes
_DIALECT_MAP = {
    "sqlite": "sqlite+aiosqlite",
    "postgres": "postgresql+asyncpg",
    "postgresql": "postgresql+asyncpg",
    "mysql": "mysql+aiomysql",
    "mariadb": "mariadb+aiomysql",
}


def _build_sa_url(engine_name: str, db_path: str | None = None, db_url: str | None = None) -> str:
    """Build SQLAlchemy connection URL from config."""
    if engine_name == "sqlite":
        path = db_path or "tune_server.db"
        return f"sqlite+aiosqlite:///{path}"
    if db_url:
        # Convert postgresql:// to postgresql+asyncpg:// if needed
        for prefix, sa_prefix in _DIALECT_MAP.items():
            if db_url.startswith(f"{prefix}://"):
                return db_url.replace(f"{prefix}://", f"{sa_prefix}://", 1)
        return db_url
    raise ValueError(f"No db_url provided for engine '{engine_name}'")


class SADatabase:
    """Database-independent async engine using SQLAlchemy Core.

    Implements DatabaseProtocol so all existing raw SQL code continues to work.
    Also provides SA-native methods for new code using Table expressions.
    """

    def __init__(
        self,
        engine_name: str = "sqlite",
        db_path: str | None = None,
        db_url: str | None = None,
        pool_min: int = 2,
        pool_max: int = 10,
    ) -> None:
        self.engine_name = engine_name
        self._sa_url = _build_sa_url(engine_name, db_path, db_url)
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._engine: AsyncEngine | None = None
        self.fts: FTSPlugin | None = None

    @property
    def sa_engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._engine

    async def connect(self) -> None:
        """Create the async engine, initialize schema, and set up FTS."""
        engine_kwargs: dict[str, Any] = {}

        if self.engine_name == "sqlite":
            # SQLite: single connection, PRAGMAs via connect event
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        else:
            # PostgreSQL/MySQL: connection pool
            engine_kwargs["pool_size"] = self._pool_max
            engine_kwargs["pool_pre_ping"] = True

        self._engine = create_async_engine(self._sa_url, **engine_kwargs)

        # Set SQLite PRAGMAs
        if self.engine_name == "sqlite":
            from sqlalchemy import event

            @event.listens_for(self._engine.sync_engine, "connect")
            def _set_sqlite_pragmas(dbapi_conn, _):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()

        # Create tables from SA metadata
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

        # Set up FTS plugin
        self.fts = create_fts_plugin(self.engine_name)
        async with self._engine.begin() as conn:
            await self.fts.setup(conn)

        logger.info("database_connected", engine=self.engine_name,
                     url=self._sa_url.split("@")[-1] if "@" in self._sa_url else self._sa_url)

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            logger.info("database_closed")

    # -----------------------------------------------------------------------
    # DatabaseProtocol methods — backward compatibility with raw SQL
    # -----------------------------------------------------------------------

    async def execute(self, sql: str, params: tuple = ()) -> ExecuteResult:
        """Execute a raw SQL statement. Supports ? placeholders (auto-translated)."""
        text_sql, bound_params = self._prepare_raw(sql, params)
        async with self._engine.begin() as conn:
            result = await conn.execute(sa.text(text_sql), bound_params)
            lastrowid = None
            is_insert = text_sql.strip().upper().startswith("INSERT")
            if result.returns_rows:
                row = result.first()
                if row and "id" in row._mapping:
                    lastrowid = row._mapping["id"]
            elif is_insert:
                try:
                    lastrowid = result.lastrowid
                except (AttributeError, Exception):
                    pass
                if not lastrowid:
                    try:
                        if result.inserted_primary_key:
                            lastrowid = result.inserted_primary_key[0]
                    except (AttributeError, Exception):
                        pass
            try:
                rowcount = result.rowcount
            except (AttributeError, Exception):
                rowcount = 0
            return ExecuteResult(lastrowid=lastrowid, rowcount=rowcount)

    async def executemany(self, sql: str, params_seq: list[tuple]) -> None:
        """Execute a statement with multiple parameter sets."""
        text_sql = self._translate_placeholders(sql)
        param_names = re.findall(r":p(\d+)", text_sql)
        async with self._engine.begin() as conn:
            for params in params_seq:
                bound = {f"p{i+1}": v for i, v in enumerate(params)}
                await conn.execute(sa.text(text_sql), bound)

    async def fetchone(self, sql: str, params: tuple = ()) -> Any | None:
        """Fetch a single row. Returns a mapping (row['column']) or None."""
        text_sql, bound_params = self._prepare_raw(sql, params)
        async with self._engine.connect() as conn:
            result = await conn.execute(sa.text(text_sql), bound_params)
            row = result.first()
            return row._mapping if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[Any]:
        """Fetch all rows. Returns list of mappings."""
        text_sql, bound_params = self._prepare_raw(sql, params)
        async with self._engine.connect() as conn:
            result = await conn.execute(sa.text(text_sql), bound_params)
            return [row._mapping for row in result.all()]

    async def commit(self) -> None:
        """No-op — SA engine.begin() auto-commits."""
        pass

    async def executescript(self, sql: str) -> None:
        """Execute a multi-statement SQL script."""
        async with self._engine.begin() as conn:
            for stmt in self._split_statements(sql):
                stmt = stmt.strip()
                if stmt and not stmt.startswith("--"):
                    try:
                        await conn.execute(sa.text(stmt))
                    except Exception:
                        pass  # Idempotent: ignore already-exists errors

    # -----------------------------------------------------------------------
    # SA-native methods — for new code using Table expressions
    # -----------------------------------------------------------------------

    async def sa_execute(self, stmt) -> ExecuteResult:
        """Execute a SA Core statement (INSERT, UPDATE, DELETE)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            lastrowid = None
            try:
                if result.inserted_primary_key:
                    lastrowid = result.inserted_primary_key[0]
            except Exception:
                pass
            try:
                rowcount = result.rowcount
            except Exception:
                rowcount = 0
            return ExecuteResult(lastrowid=lastrowid, rowcount=rowcount)

    async def sa_fetchone(self, stmt) -> Any | None:
        """Execute a SA Core SELECT and return one row mapping."""
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            row = result.first()
            return row._mapping if row else None

    async def sa_fetchall(self, stmt) -> list[Any]:
        """Execute a SA Core SELECT and return all row mappings."""
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            return [row._mapping for row in result.all()]

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _prepare_raw(self, sql: str, params: tuple) -> tuple[str, dict]:
        """Translate ? placeholders to :p1, :p2 and build param dict."""
        text_sql = self._translate_placeholders(sql)
        bound = {f"p{i+1}": v for i, v in enumerate(params)}
        return text_sql, bound

    @staticmethod
    def _translate_placeholders(sql: str) -> str:
        """Replace ? placeholders with :p1, :p2, ... (outside string literals)."""
        result = []
        counter = 0
        in_string = False
        i = 0
        while i < len(sql):
            ch = sql[i]
            if ch == "'" and (i == 0 or sql[i - 1] != "\\"):
                in_string = not in_string
                result.append(ch)
            elif ch == "?" and not in_string:
                counter += 1
                result.append(f":p{counter}")
            else:
                result.append(ch)
            i += 1
        return "".join(result)

    @staticmethod
    def _split_statements(sql: str) -> list[str]:
        """Split SQL script into individual statements, respecting $$ blocks."""
        statements = []
        current = []
        in_dollar = False
        for line in sql.split("\n"):
            stripped = line.strip()
            if "$$" in stripped:
                in_dollar = not in_dollar
            current.append(line)
            if stripped.endswith(";") and not in_dollar:
                statements.append("\n".join(current))
                current = []
        if current:
            statements.append("\n".join(current))
        return statements

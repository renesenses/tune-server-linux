from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from tune_server.db.engine import ExecuteResult

logger = structlog.get_logger()

_SCHEMA_PATH = Path(__file__).parent / "schema_postgres.sql"


class PostgresDatabase:
    engine_name = "postgres"

    def __init__(self, db_url: str, pool_min: int = 2, pool_max: int = 10) -> None:
        self._db_url = db_url
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._pool = None

    async def connect(self) -> None:
        import asyncpg

        logger.info("database_connecting", url=self._db_url.split("@")[-1], engine="postgres")
        self._pool = await asyncpg.create_pool(
            self._db_url, min_size=self._pool_min, max_size=self._pool_max,
        )
        await self._init_schema()
        logger.info("database_connected", engine="postgres")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("database_closed")

    async def execute(self, sql: str, params: tuple = ()) -> ExecuteResult:
        translated = self._translate_params(sql)
        async with self._pool.acquire() as conn:
            if "RETURNING" in sql.upper():
                row = await conn.fetchrow(translated, *params)
                return ExecuteResult(
                    lastrowid=row["id"] if row else None,
                    rowcount=1,
                )
            result = await conn.execute(translated, *params)
            # asyncpg returns e.g. "DELETE 3" or "UPDATE 1" or "INSERT 0 1"
            rowcount = 0
            if result:
                parts = result.split()
                if parts:
                    try:
                        rowcount = int(parts[-1])
                    except ValueError:
                        pass
            return ExecuteResult(lastrowid=None, rowcount=rowcount)

    async def executemany(self, sql: str, params_seq: list[tuple]) -> None:
        translated = self._translate_params(sql)
        async with self._pool.acquire() as conn:
            await conn.executemany(translated, params_seq)

    async def fetchone(self, sql: str, params: tuple = ()) -> Any | None:
        translated = self._translate_params(sql)
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(translated, *params)

    async def fetchall(self, sql: str, params: tuple = ()) -> list[Any]:
        translated = self._translate_params(sql)
        async with self._pool.acquire() as conn:
            return await conn.fetch(translated, *params)

    async def commit(self) -> None:
        pass  # asyncpg auto-commits; transactions handled per-statement

    async def executescript(self, sql: str) -> None:
        statements = self._split_sql(sql)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for statement in statements:
                    await conn.execute(statement)

    @staticmethod
    def _split_sql(sql: str) -> list[str]:
        """Split SQL script into statements, respecting $$ blocks and strings."""
        statements = []
        current = []
        in_dollar = False
        lines = sql.split("\n")
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            # Track $$ dollar-quoted blocks
            count = stripped.count("$$")
            current.append(line)
            if count % 2 == 1:
                in_dollar = not in_dollar
            if not in_dollar and stripped.endswith(";"):
                stmt = "\n".join(current).strip().rstrip(";").strip()
                if stmt:
                    statements.append(stmt)
                current = []
        if current:
            stmt = "\n".join(current).strip().rstrip(";").strip()
            if stmt:
                statements.append(stmt)
        return statements

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def _init_schema(self) -> None:
        if _SCHEMA_PATH.exists():
            schema_sql = _SCHEMA_PATH.read_text()
            await self.executescript(schema_sql)
        await self._run_migrations()
        logger.info("database_schema_initialized", engine="postgres")

    async def _run_migrations(self) -> None:
        """Run safe column additions for schema evolution."""
        migrations = [
            "ALTER TABLE tracks ADD COLUMN IF NOT EXISTS file_mtime REAL",
            "ALTER TABLE zones ADD COLUMN IF NOT EXISTS queue_json TEXT",
            "ALTER TABLE tracks ADD COLUMN IF NOT EXISTS audio_hash TEXT",
            "ALTER TABLE zones ADD COLUMN IF NOT EXISTS sync_delay_ms INTEGER DEFAULT 0",
        ]
        async with self._pool.acquire() as conn:
            for sql in migrations:
                try:
                    await conn.execute(sql)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Parameter Translation
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_params(sql: str) -> str:
        """Replace ? placeholders with $1, $2, $3... for asyncpg."""
        parts = []
        counter = 0
        in_string = False
        i = 0
        while i < len(sql):
            ch = sql[i]
            if ch == "'" and (i == 0 or sql[i - 1] != "\\"):
                in_string = not in_string
                parts.append(ch)
            elif ch == "?" and not in_string:
                counter += 1
                parts.append(f"${counter}")
            else:
                parts.append(ch)
            i += 1
        return "".join(parts)

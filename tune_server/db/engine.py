from __future__ import annotations

from pathlib import Path

import aiosqlite
import structlog

logger = structlog.get_logger()

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._db

    async def connect(self) -> None:
        logger.info("database_connecting", path=self._db_path)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row

        # Enable WAL mode for concurrent reads
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA synchronous=NORMAL")

        await self._init_schema()
        logger.info("database_connected", path=self._db_path)

    async def _init_schema(self) -> None:
        schema_sql = _SCHEMA_PATH.read_text()
        await self._db.executescript(schema_sql)
        await self._db.commit()
        await self._run_migrations()
        logger.info("database_schema_initialized")

    async def _run_migrations(self) -> None:
        """Run safe column additions and table creations for schema evolution."""
        migrations = [
            "ALTER TABLE tracks ADD COLUMN file_mtime REAL",
            "ALTER TABLE zones ADD COLUMN queue_json TEXT",
        ]
        for sql in migrations:
            try:
                await self._db.execute(sql)
                await self._db.commit()
            except Exception:
                pass  # Column already exists

        # Table migrations (idempotent via IF NOT EXISTS)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS network_mounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host TEXT NOT NULL,
                share_name TEXT NOT NULL,
                protocol TEXT NOT NULL,
                mount_path TEXT NOT NULL,
                username TEXT,
                password TEXT,
                auto_mount INTEGER DEFAULT 1,
                status TEXT DEFAULT 'unmounted',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(host, share_name, protocol)
            )
        """)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("database_closed")

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        return await self.connection.execute(sql, params)

    async def executemany(self, sql: str, params_seq: list[tuple]) -> aiosqlite.Cursor:
        return await self.connection.executemany(sql, params_seq)

    async def fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        cursor = await self.connection.execute(sql, params)
        return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        cursor = await self.connection.execute(sql, params)
        return await cursor.fetchall()

    async def commit(self) -> None:
        await self.connection.commit()

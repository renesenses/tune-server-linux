"""SQLAlchemy Core async engine — database-independent wrapper.

Implements DatabaseProtocol for backward compatibility with existing raw SQL
code while providing SA-native methods for new code.

Supports SQLite (aiosqlite), PostgreSQL (asyncpg), and any SA-supported async dialect.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import structlog

from tune_server.db.engine import ExecuteResult
from tune_server.db.tables import metadata
from tune_server.db.fts import create_fts_plugin, FTSPlugin

logger = structlog.get_logger()


class _SARow:
    """Row wrapper supporting both row["key"] and row[0] access.

    SA RowMapping only supports key access. This wrapper adds integer index
    access for backward compatibility with code like `row[0]`.
    """

    def __init__(self, mapping):
        self._mapping = mapping
        self._values = list(mapping.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def __contains__(self, key):
        return key in self._mapping

    def get(self, key, default=None):
        return self._mapping.get(key, default)

    def keys(self):
        return self._mapping.keys()

    def values(self):
        return self._mapping.values()

    def items(self):
        return self._mapping.items()

    def __repr__(self):
        return f"_SARow({dict(self._mapping)})"

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
        self._db_path = db_path  # raw SQLite path (None for postgres)
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
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

        # Backup the SQLite file BEFORE any schema changes so a botched
        # migration is recoverable. PostgreSQL users have pg_dump; we don't
        # try to manage that.
        self._backup_sqlite_if_needed()

        # Create tables from SA metadata
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

        # Migrate: add missing columns to existing tables
        await self._run_column_migrations()

        # Migrate: drop FK on zones.output_device_id (SQLite requires table rebuild)
        if self.engine_name == "sqlite":
            await self._migrate_zones_drop_fk()

        # Set up FTS plugin
        self.fts = create_fts_plugin(self.engine_name)
        async with self._engine.begin() as conn:
            await self.fts.setup(conn)

        logger.info("database_connected", engine=self.engine_name,
                     url=self._sa_url.split("@")[-1] if "@" in self._sa_url else self._sa_url)

    def _backup_sqlite_if_needed(self) -> None:
        """Snapshot the SQLite DB before migrations run.

        Delegates to the shared backup module (``tune_server.db.backup``)
        which handles timestamped copies, WAL/SHM files, and rotation.
        Skipped for in-memory and PostgreSQL. Failures are non-fatal.
        """
        if self.engine_name != "sqlite":
            return
        if not self._db_path or self._db_path == ":memory:":
            return

        from pathlib import Path
        src = Path(self._db_path)
        if not src.exists() or src.stat().st_size == 0:
            return  # Nothing to back up — fresh install

        from tune_server.db.backup import create_backup
        try:
            result = create_backup(self._db_path)
            if result:
                logger.info("database_pre_migration_backup", **result)
        except Exception as e:
            logger.warning("database_backup_failed", error=str(e), path=str(src))

    async def _run_column_migrations(self) -> None:
        """Add missing columns to existing tables.

        SA's create_all only creates *missing* tables — it never touches an
        existing table's schema. We compare the live schema with the SA
        metadata and add any column the model declares but the DB lacks,
        each in its own transaction (SQLite/asyncpg abort the whole tx on
        the first error). This is bulletproof against future column
        additions: any new sa.Column in tables.py is auto-migrated.
        """
        from sqlalchemy import inspect as sa_inspect

        def _live_schema(sync_conn):
            insp = sa_inspect(sync_conn)
            existing_tables = set(insp.get_table_names())
            return {
                table_name: {c["name"] for c in insp.get_columns(table_name)}
                for table_name in existing_tables
            }

        async with self._engine.begin() as conn:
            live = await conn.run_sync(_live_schema)

        for table in metadata.tables.values():
            if table.name not in live:
                continue  # create_all should have created it
            existing_cols = live[table.name]
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                # Compile column type for the active dialect
                try:
                    coltype = col.type.compile(dialect=self._engine.dialect)
                except Exception:
                    coltype = "TEXT"
                sql = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}"
                try:
                    async with self._engine.begin() as conn:
                        await conn.execute(sa.text(sql))
                    logger.info(
                        "migration_added_column",
                        table=table.name,
                        column=col.name,
                        type=coltype,
                    )
                except Exception as e:
                    logger.warning(
                        "migration_column_failed",
                        table=table.name,
                        column=col.name,
                        error=str(e),
                    )

        # Track credits table (idempotent via CREATE TABLE IF NOT EXISTS in SA metadata.create_all)
        # Ensure indexes exist for older databases
        index_sqls = [
            "CREATE INDEX IF NOT EXISTS idx_track_credits_track ON track_credits(track_id)",
            "CREATE INDEX IF NOT EXISTS idx_track_credits_artist ON track_credits(artist_id)",
        ]
        async with self._engine.begin() as conn:
            for sql in index_sqls:
                try:
                    await conn.execute(sa.text(sql))
                except Exception:
                    pass

    async def _migrate_zones_drop_fk(self) -> None:
        """Remove FK constraint on zones.output_device_id for existing SQLite DBs."""
        async with self._engine.begin() as conn:
            # Check if the FK exists by inspecting the CREATE TABLE statement
            result = await conn.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name='zones'")
            )
            row = result.first()
            if not row or not row[0]:
                return
            create_sql = row[0]
            if "output_devices" not in create_sql:
                return  # FK already removed or never existed

            logger.info("migrating_zones_drop_fk")
            # SQLite: rebuild table without FK
            await conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
            await conn.execute(sa.text("""
                CREATE TABLE zones_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    output_type TEXT NOT NULL DEFAULT 'local',
                    output_device_id TEXT,
                    volume REAL DEFAULT 0.5,
                    group_id TEXT,
                    sync_delay_ms INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    queue_json TEXT,
                    muted BOOLEAN DEFAULT FALSE,
                    online BOOLEAN DEFAULT TRUE
                )
            """))
            await conn.execute(sa.text(
                "INSERT INTO zones_new SELECT id, name, output_type, output_device_id, "
                "volume, group_id, sync_delay_ms, created_at, "
                "COALESCE(queue_json, NULL), "
                "COALESCE(muted, FALSE), COALESCE(online, TRUE) FROM zones"
            ))
            await conn.execute(sa.text("DROP TABLE zones"))
            await conn.execute(sa.text("ALTER TABLE zones_new RENAME TO zones"))
            await conn.execute(sa.text("PRAGMA foreign_keys=ON"))
            logger.info("migrated_zones_drop_fk")

    def list_backups(self) -> list[dict]:
        """List available backups, newest first."""
        from datetime import datetime
        # Extract DB path from URL
        if "sqlite" in self._sa_url:
            db_path = self._sa_url.split("///")[-1]
        else:
            return []  # Backups only for SQLite
        db_file = Path(db_path)
        backup_dir = db_file.parent / "backups"
        if not backup_dir.exists():
            return []
        backups = sorted(backup_dir.glob(f"{db_file.stem}_*{db_file.suffix}"), reverse=True)
        result = []
        for b in backups:
            stat = b.stat()
            result.append({
                "filename": b.name,
                "size": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        return result

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

    @staticmethod
    def _is_write_statement(sql: str) -> bool:
        """True if the SQL mutates state (INSERT/UPDATE/DELETE) and so
        must run inside a transaction that commits — including paths
        like INSERT ... RETURNING which return rows but still write."""
        head = sql.lstrip().split(None, 1)
        if not head:
            return False
        return head[0].upper() in {"INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE"}

    async def fetchone(self, sql: str, params: tuple = ()) -> Any | None:
        """Fetch a single row. Returns _SARow (supports row['key'] and row[0]).

        Auto-detects mutating statements (INSERT/UPDATE/DELETE — including
        the RETURNING form) and runs them under engine.begin() so they
        actually commit. Plain SELECTs stay on engine.connect() to avoid
        starting unnecessary transactions.
        """
        text_sql, bound_params = self._prepare_raw(sql, params)
        ctx = self._engine.begin() if self._is_write_statement(text_sql) else self._engine.connect()
        async with ctx as conn:
            result = await conn.execute(sa.text(text_sql), bound_params)
            row = result.first()
            return _SARow(row._mapping) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[Any]:
        """Fetch all rows. Returns list of _SARow."""
        text_sql, bound_params = self._prepare_raw(sql, params)
        ctx = self._engine.begin() if self._is_write_statement(text_sql) else self._engine.connect()
        async with ctx as conn:
            result = await conn.execute(sa.text(text_sql), bound_params)
            return [_SARow(row._mapping) for row in result.all()]

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
        """Execute a SA Core SELECT and return one row.

        Returns a _SARow that supports both row["key"] and row[0] access.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            row = result.first()
            return _SARow(row._mapping) if row else None

    async def sa_fetchall(self, stmt) -> list[Any]:
        """Execute a SA Core SELECT and return all rows."""
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            return [_SARow(row._mapping) for row in result.all()]

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

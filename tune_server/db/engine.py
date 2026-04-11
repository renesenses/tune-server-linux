from __future__ import annotations

import dataclasses
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import aiosqlite
import structlog

logger = structlog.get_logger()

_SCHEMA_PATH = Path(__file__).parent / "schema_sqlite.sql"
_MAX_BACKUPS = 5


# ---------------------------------------------------------------------------
# Database Protocol & Result
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ExecuteResult:
    """Result of an execute() call, portable across engines."""
    lastrowid: int | None = None
    rowcount: int = 0


@runtime_checkable
class DatabaseProtocol(Protocol):
    @property
    def engine_name(self) -> str: ...
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def execute(self, sql: str, params: tuple = ()) -> ExecuteResult: ...
    async def executemany(self, sql: str, params_seq: list[tuple]) -> None: ...
    async def fetchone(self, sql: str, params: tuple = ()) -> Any | None: ...
    async def fetchall(self, sql: str, params: tuple = ()) -> list[Any]: ...
    async def commit(self) -> None: ...
    async def executescript(self, sql: str) -> None: ...


# ---------------------------------------------------------------------------
# SQLite Implementation
# ---------------------------------------------------------------------------

class SQLiteDatabase:
    engine_name = "sqlite"

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._db

    async def connect(self) -> None:
        logger.info("database_connecting", path=self._db_path, engine="sqlite")

        # Backup database before any schema changes
        self._backup_database()

        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row

        # Enable WAL mode for concurrent reads
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA synchronous=NORMAL")

        await self._init_schema()
        logger.info("database_connected", path=self._db_path, engine="sqlite")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("database_closed")

    async def execute(self, sql: str, params: tuple = ()) -> ExecuteResult:
        cursor = await self.connection.execute(sql, params)
        lastrowid = cursor.lastrowid
        rowcount = cursor.rowcount
        # RETURNING clause produces rows that must be consumed before commit
        if "RETURNING" in sql.upper():
            row = await cursor.fetchone()
            if row and lastrowid is None:
                lastrowid = row[0]
        return ExecuteResult(lastrowid=lastrowid, rowcount=rowcount)

    async def executemany(self, sql: str, params_seq: list[tuple]) -> None:
        await self.connection.executemany(sql, params_seq)

    async def fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        cursor = await self.connection.execute(sql, params)
        return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        cursor = await self.connection.execute(sql, params)
        return await cursor.fetchall()

    async def commit(self) -> None:
        await self.connection.commit()

    async def executescript(self, sql: str) -> None:
        await self.connection.executescript(sql)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def _init_schema(self) -> None:
        schema_sql = _SCHEMA_PATH.read_text()
        await self.executescript(schema_sql)
        await self.commit()
        await self._run_migrations()
        logger.info("database_schema_initialized")

    async def _run_migrations(self) -> None:
        """Run safe column additions and table creations for schema evolution."""
        migrations = [
            "ALTER TABLE tracks ADD COLUMN file_mtime REAL",
            "ALTER TABLE zones ADD COLUMN queue_json TEXT",
            "ALTER TABLE tracks ADD COLUMN audio_hash TEXT",
            "ALTER TABLE zones ADD COLUMN sync_delay_ms INTEGER DEFAULT 0",
            "ALTER TABLE tracks ADD COLUMN isrc TEXT",
        ]
        for sql in migrations:
            try:
                await self.connection.execute(sql)
                await self.commit()
            except Exception:
                pass  # Column already exists

        # Table migrations (idempotent via IF NOT EXISTS)
        await self.connection.execute("""
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
        await self.commit()

        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS device_credentials (
                device_id TEXT PRIMARY KEY,
                device_name TEXT,
                credentials TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.commit()

        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS radio_stations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                stream_url TEXT NOT NULL,
                logo_url TEXT,
                genre TEXT,
                tags TEXT,
                codec TEXT,
                country TEXT,
                homepage_url TEXT,
                favorite INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.commit()

        # User profiles & favorites
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                avatar_color TEXT DEFAULT '#FF6B35',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS user_favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
                track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
                album_id INTEGER REFERENCES albums(id) ON DELETE CASCADE,
                artist_id INTEGER REFERENCES artists(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.commit()

        # Playlist manager tables
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS playlist_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                service TEXT NOT NULL,
                service_playlist_id TEXT NOT NULL,
                service_playlist_name TEXT,
                sync_direction TEXT NOT NULL DEFAULT 'pull',
                last_synced_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(local_playlist_id, service, service_playlist_id)
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS playlist_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_service TEXT NOT NULL,
                source_playlist_id TEXT NOT NULL,
                playlist_name TEXT NOT NULL,
                track_count INTEGER DEFAULT 0,
                snapshot_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS transfer_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation TEXT NOT NULL,
                source_service TEXT NOT NULL,
                source_playlist_id TEXT,
                source_playlist_name TEXT,
                target_service TEXT,
                target_playlist_id TEXT,
                target_playlist_name TEXT,
                total_tracks INTEGER DEFAULT 0,
                matched INTEGER DEFAULT 0,
                approximate INTEGER DEFAULT 0,
                not_found INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'completed',
                details TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS sync_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_link_id INTEGER NOT NULL REFERENCES playlist_links(id) ON DELETE CASCADE,
                interval_minutes INTEGER NOT NULL DEFAULT 1440,
                last_run_at TIMESTAMP,
                next_run_at TIMESTAMP,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(playlist_link_id)
            )
        """)
        await self.commit()

    # ------------------------------------------------------------------
    # Backup (SQLite-specific, file-based)
    # ------------------------------------------------------------------

    def _backup_database(self) -> None:
        """Create a timestamped backup of the database, keeping last N backups."""
        db_file = Path(self._db_path)
        if not db_file.exists():
            return

        backup_dir = db_file.parent / "backups"
        backup_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"{db_file.stem}_{timestamp}{db_file.suffix}"

        try:
            shutil.copy2(str(db_file), str(backup_path))
            # Also copy WAL and SHM files if they exist
            for suffix in ("-wal", "-shm"):
                wal_file = db_file.with_name(db_file.name + suffix)
                if wal_file.exists():
                    shutil.copy2(str(wal_file), str(backup_path) + suffix)
            logger.info("database_backup_created", path=str(backup_path))
        except Exception:
            logger.exception("database_backup_error")
            return

        # Prune old backups, keep only the last N
        backups = sorted(backup_dir.glob(f"{db_file.stem}_*{db_file.suffix}"))
        while len(backups) > _MAX_BACKUPS:
            old = backups.pop(0)
            try:
                old.unlink()
                for suffix in ("-wal", "-shm"):
                    wal = old.with_name(old.name + suffix)
                    if wal.exists():
                        wal.unlink()
                logger.info("database_backup_pruned", path=str(old))
            except Exception:
                pass

    def list_backups(self) -> list[dict]:
        """List available backups, newest first."""
        db_file = Path(self._db_path)
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

    def create_backup(self) -> dict | None:
        """Manually create a backup. Returns backup info or None on error."""
        self._backup_database()
        backups = self.list_backups()
        return backups[0] if backups else None

    async def restore_backup(self, filename: str) -> bool:
        """Restore database from a backup file. Closes and reopens connection."""
        db_file = Path(self._db_path)
        backup_dir = db_file.parent / "backups"
        backup_path = backup_dir / filename

        if not backup_path.exists():
            return False

        # Validate filename to prevent path traversal
        if backup_path.parent.resolve() != backup_dir.resolve():
            return False

        try:
            if self._db:
                await self._db.close()
                self._db = None

            for suffix in ("-wal", "-shm"):
                wal = db_file.with_name(db_file.name + suffix)
                if wal.exists():
                    wal.unlink()

            shutil.copy2(str(backup_path), str(db_file))
            logger.info("database_restored", backup=filename)

            await self.connect()
            return True
        except Exception:
            logger.exception("database_restore_error", backup=filename)
            try:
                await self.connect()
            except Exception:
                pass
            return False


# Backward-compatible alias
Database = SQLiteDatabase

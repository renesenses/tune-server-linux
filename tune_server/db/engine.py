from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import aiosqlite
import structlog

logger = structlog.get_logger()

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_MAX_BACKUPS = 5


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

        # Backup database before any schema changes
        self._backup_database()

        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row

        # Enable WAL mode for concurrent reads
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA synchronous=NORMAL")

        await self._init_schema()
        logger.info("database_connected", path=self._db_path)

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
                # Remove associated WAL/SHM files
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
            # Close current connection
            if self._db:
                await self._db.close()
                self._db = None

            # Copy backup over current DB (remove WAL/SHM first for clean state)
            for suffix in ("-wal", "-shm"):
                wal = db_file.with_name(db_file.name + suffix)
                if wal.exists():
                    wal.unlink()

            shutil.copy2(str(backup_path), str(db_file))
            logger.info("database_restored", backup=filename)

            # Reconnect
            await self.connect()
            return True
        except Exception:
            logger.exception("database_restore_error", backup=filename)
            # Try to reconnect anyway
            try:
                await self.connect()
            except Exception:
                pass
            return False

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
            "ALTER TABLE tracks ADD COLUMN audio_hash TEXT",
            "ALTER TABLE zones ADD COLUMN sync_delay_ms INTEGER DEFAULT 0",
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

        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS device_credentials (
                device_id TEXT PRIMARY KEY,
                device_name TEXT,
                credentials TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._db.commit()

        await self._db.execute("""
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

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
        # Wait up to 5s if another connection holds the write lock — Windows
        # is especially sensitive: scanner + API + websocket can hit the DB
        # at the same time and SQLITE_BUSY surfaces as a 500.
        await self._db.execute("PRAGMA busy_timeout=5000")

        await self._init_schema()
        logger.info("database_connected", path=self._db_path, engine="sqlite")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("database_closed")

    async def execute(self, sql: str, params: tuple = ()) -> ExecuteResult:
        try:
            cursor = await self.connection.execute(sql, params)
        except aiosqlite.Error as e:
            logger.error("sqlite_execute_failed", error=str(e), sql=sql[:200])
            raise
        lastrowid = cursor.lastrowid
        rowcount = cursor.rowcount
        # RETURNING clause produces rows that must be consumed before commit
        if "RETURNING" in sql.upper():
            row = await cursor.fetchone()
            if row and lastrowid is None:
                lastrowid = row[0]
        return ExecuteResult(lastrowid=lastrowid, rowcount=rowcount)

    async def executemany(self, sql: str, params_seq: list[tuple]) -> None:
        try:
            await self.connection.executemany(sql, params_seq)
        except aiosqlite.Error as e:
            logger.error("sqlite_executemany_failed", error=str(e), sql=sql[:200], rows=len(params_seq))
            raise

    async def fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        try:
            cursor = await self.connection.execute(sql, params)
        except aiosqlite.Error as e:
            logger.error("sqlite_fetchone_failed", error=str(e), sql=sql[:200])
            raise
        return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        try:
            cursor = await self.connection.execute(sql, params)
        except aiosqlite.Error as e:
            logger.error("sqlite_fetchall_failed", error=str(e), sql=sql[:200])
            raise
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
            # Metadata manager columns
            "ALTER TABLE tracks ADD COLUMN genre TEXT",
            "ALTER TABLE tracks ADD COLUMN composer TEXT",
            "ALTER TABLE tracks ADD COLUMN year INTEGER",
            "ALTER TABLE tracks ADD COLUMN lyrics TEXT",
            "ALTER TABLE tracks ADD COLUMN synced_lyrics TEXT",
            "ALTER TABLE tracks ADD COLUMN comment TEXT",
            "ALTER TABLE tracks ADD COLUMN musicbrainz_recording_id TEXT",
            "ALTER TABLE tracks ADD COLUMN acoustid TEXT",
            "ALTER TABLE tracks ADD COLUMN bpm REAL",
            "ALTER TABLE tracks ADD COLUMN label TEXT",
            "ALTER TABLE tracks ADD COLUMN custom_tags TEXT",
            "ALTER TABLE albums ADD COLUMN musicbrainz_release_id TEXT",
            "ALTER TABLE albums ADD COLUMN label TEXT",
            "ALTER TABLE albums ADD COLUMN catalog_number TEXT",
            "ALTER TABLE albums ADD COLUMN barcode TEXT",
            # Artist metadata
            "ALTER TABLE artists ADD COLUMN musicbrainz_id TEXT",
            "ALTER TABLE artists ADD COLUMN discogs_id TEXT",
            "ALTER TABLE artists ADD COLUMN bio TEXT",
            "ALTER TABLE artists ADD COLUMN image_path TEXT",
            "ALTER TABLE artists ADD COLUMN sort_name TEXT",
            # Album quality columns
            "ALTER TABLE albums ADD COLUMN format TEXT",
            "ALTER TABLE albums ADD COLUMN sample_rate INTEGER",
            "ALTER TABLE albums ADD COLUMN bit_depth INTEGER",
            "ALTER TABLE albums ADD COLUMN artist_name TEXT",
            "ALTER TABLE albums ADD COLUMN bio TEXT",
            # Waveform analysis columns
            "ALTER TABLE tracks ADD COLUMN waveform_data TEXT",
            "ALTER TABLE tracks ADD COLUMN waveform_generated_at TIMESTAMP",
            "ALTER TABLE tracks ADD COLUMN loudness_lufs REAL",
            # Disc subtitle (multi-disc album sub-names)
            "ALTER TABLE tracks ADD COLUMN disc_subtitle TEXT",
            # MusicBrainz IDs from audio tags
            "ALTER TABLE albums ADD COLUMN musicbrainz_release_id TEXT",
            "ALTER TABLE albums ADD COLUMN musicbrainz_release_group_id TEXT",
            "ALTER TABLE tracks ADD COLUMN musicbrainz_recording_id TEXT",
            # Original year (TDOR/ORIGINALDATE) vs release year (TDRL/DATE)
            "ALTER TABLE albums ADD COLUMN original_year INTEGER",
            # Full ISO 8601 release dates (display complement to year/original_year)
            "ALTER TABLE albums ADD COLUMN release_date TEXT",
            "ALTER TABLE albums ADD COLUMN original_date TEXT",
            # Artist image source tracking (priority: user > discogs > musicbrainz > wikipedia)
            "ALTER TABLE artists ADD COLUMN image_source TEXT",
            "ALTER TABLE zones ADD COLUMN was_playing INTEGER DEFAULT 0",
            "ALTER TABLE zones ADD COLUMN last_position_ms INTEGER DEFAULT 0",
            # Multi-user profile columns
            "ALTER TABLE user_profiles ADD COLUMN avatar_url TEXT",
            "ALTER TABLE user_profiles ADD COLUMN pin_hash TEXT",
            "ALTER TABLE user_profiles ADD COLUMN is_admin INTEGER DEFAULT 0",
            "ALTER TABLE user_profiles ADD COLUMN eq_settings TEXT",
            "ALTER TABLE user_profiles ADD COLUMN quality_preference TEXT",
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
                avatar_url TEXT,
                pin_hash TEXT,
                is_admin INTEGER DEFAULT 0,
                eq_settings TEXT,
                quality_preference TEXT,
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

        # Metadata manager tables
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS metadata_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
                album_id INTEGER REFERENCES albums(id) ON DELETE CASCADE,
                field TEXT NOT NULL,
                current_value TEXT,
                suggested_value TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS metadata_fix_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                tracks_scanned INTEGER DEFAULT 0,
                auto_fixed INTEGER DEFAULT 0,
                suggestions INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                details TEXT
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS duplicate_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id_a INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                track_id_b INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                audio_hash TEXT NOT NULL,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved INTEGER DEFAULT 0
            )
        """)
        await self.commit()

        # Zone manager tables
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS zone_groups (
                id TEXT PRIMARY KEY,
                name TEXT,
                leader_zone_id INTEGER NOT NULL,
                master_volume REAL DEFAULT 0.5,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS zone_group_members (
                group_id TEXT NOT NULL REFERENCES zone_groups(id) ON DELETE CASCADE,
                zone_id INTEGER NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
                volume_offset REAL DEFAULT 0.0,
                muted INTEGER DEFAULT 0,
                PRIMARY KEY(group_id, zone_id)
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS zone_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                config TEXT NOT NULL,
                icon TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.commit()

        # Zone column migrations
        migrations_zone = [
            "ALTER TABLE zones ADD COLUMN muted INTEGER DEFAULT 0",
            "ALTER TABLE zones ADD COLUMN online INTEGER DEFAULT 1",
            "ALTER TABLE zones ADD COLUMN stereo_pair_id TEXT",
            "ALTER TABLE zones ADD COLUMN stereo_channel TEXT",
        ]
        for sql in migrations_zone:
            try:
                await self.connection.execute(sql)
                await self.commit()
            except Exception:
                pass

        # Playlist manager tables
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS playlist_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                service TEXT NOT NULL,
                service_playlist_id TEXT NOT NULL,
                service_playlist_name TEXT,
                sync_direction TEXT NOT NULL DEFAULT 'pull',
                sync_interval_minutes INTEGER NOT NULL DEFAULT 0,
                last_synced_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(local_playlist_id, service, service_playlist_id)
            )
        """)
        # Idempotent migration for older databases created without sync_interval_minutes
        try:
            await self.connection.execute(
                "ALTER TABLE playlist_links ADD COLUMN sync_interval_minutes INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass  # Column already exists
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

        # Sync link snapshots (delta detection for bidirectional sync)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS sync_link_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_link_id INTEGER NOT NULL REFERENCES playlist_links(id) ON DELETE CASCADE,
                side TEXT NOT NULL,
                tracks_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sync_link_snapshots_link "
            "ON sync_link_snapshots(playlist_link_id, side)"
        )
        await self.commit()

        # Track credits (multiple artists per track with roles/instruments)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS track_credits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                artist_id INTEGER REFERENCES artists(id) ON DELETE SET NULL,
                artist_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'performer',
                instrument TEXT,
                position INTEGER DEFAULT 0
            )
        """)
        await self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_track_credits_track ON track_credits(track_id)"
        )
        await self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_track_credits_artist ON track_credits(artist_id)"
        )
        await self.commit()

        # Party votes (persistent collaborative votes)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS party_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id INTEGER NOT NULL,
                track_title TEXT NOT NULL,
                track_artist TEXT,
                queue_position INTEGER NOT NULL,
                vote_count INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_party_votes_zone ON party_votes(zone_id)"
        )
        await self.commit()

        # Performance indexes for library queries (idempotent)
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_albums_original_year ON albums(original_year)",
            "CREATE INDEX IF NOT EXISTS idx_tracks_disc_number ON tracks(disc_number, track_number)",
        ):
            await self.connection.execute(stmt)
        await self.commit()

        # Album ratings & notes
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS album_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id INTEGER NOT NULL,
                profile_id INTEGER,
                rating INTEGER CHECK(rating BETWEEN 1 AND 5),
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(album_id, profile_id)
            )
        """)
        await self.commit()

        # Collections (album grouping)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                icon TEXT DEFAULT 'folder',
                color TEXT DEFAULT '#6366f1',
                profile_id INTEGER,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS collection_albums (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                album_id INTEGER NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(collection_id, album_id)
            )
        """)
        await self.commit()

        # Collaborative playlists
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS collaborative_playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                created_by INTEGER,
                is_public BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS collaborative_playlist_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL REFERENCES collaborative_playlists(id) ON DELETE CASCADE,
                track_id INTEGER,
                track_title TEXT NOT NULL,
                track_artist TEXT,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                votes INTEGER DEFAULT 0
            )
        """)
        await self.commit()

        # Playback history
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS playback_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                track_id INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
                zone_id INTEGER,
                track_title TEXT,
                artist_name TEXT,
                album_title TEXT,
                cover_path TEXT,
                duration_ms INTEGER,
                listened_ms INTEGER,
                source TEXT,
                played_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_playback_history_played ON playback_history(played_at DESC)"
        )
        # Composite indexes for dashboard queries (each WHERE = played_at +
        # one filter dimension). Without these, big libraries fall back to
        # full scans on every period change.
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_playback_history_user_played ON playback_history(user_id, played_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_playback_history_zone_played ON playback_history(zone_id, played_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_playback_history_artist_played ON playback_history(artist_name, played_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_playback_history_source_played ON playback_history(source, played_at DESC)",
        ):
            await self.connection.execute(stmt)
        await self.commit()

        # User-defined tags/labels for tracks, albums, and artists
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS user_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                color TEXT NOT NULL DEFAULT '#6366f1',
                icon TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS user_tag_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_id INTEGER NOT NULL REFERENCES user_tags(id) ON DELETE CASCADE,
                item_type TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tag_id, item_type, item_id)
            )
        """)
        await self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_tag_items_tag ON user_tag_items(tag_id)"
        )
        await self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_tag_items_item ON user_tag_items(item_type, item_id)"
        )
        await self.commit()

        # Zone audio profiles (room correction / per-zone EQ)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS zone_audio_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT 'Default',
                eq_preset TEXT,
                bass_boost REAL DEFAULT 0,
                treble_boost REAL DEFAULT 0,
                loudness_compensation BOOLEAN DEFAULT 0,
                crossfeed TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(zone_id, name)
            )
        """)
        await self.commit()

    # ------------------------------------------------------------------
    # Backup (SQLite-specific, file-based)
    # ------------------------------------------------------------------

    def _backup_database(self) -> None:
        """Create a timestamped backup of the database before schema migration.

        Delegates to the shared backup module (``tune_server.db.backup``)
        which handles timestamped copies, WAL/SHM files, and rotation.
        """
        db_file = Path(self._db_path)
        if not db_file.exists():
            return

        from tune_server.db.backup import create_backup
        try:
            result = create_backup(self._db_path)
            if result:
                logger.info("database_pre_migration_backup", **result)
        except Exception:
            logger.exception("database_backup_error")

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

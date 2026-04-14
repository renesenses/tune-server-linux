# Tune Server — Database Architecture

## Overview

Tune Server supports multiple database engines through an abstraction layer. The persistence layer is designed to be **database-independent**: the same codebase runs on SQLite, PostgreSQL, and (planned) MySQL/MariaDB.

## Architecture

```
                    +-------------------+
                    |   API Routes      |
                    |   (FastAPI)       |
                    +--------+----------+
                             |
                    +--------v----------+
                    |   Repositories    |
                    |   (8 repos)       |
                    +--------+----------+
                             |
                    +--------v----------+
                    |  DatabaseProtocol |
                    |  (abstract)       |
                    +---+----------+----+
                        |          |
               +--------v---+  +--v-----------+
               | SQLite      |  | PostgreSQL   |
               | (aiosqlite) |  | (asyncpg)    |
               +-------------+  +--------------+
```

## Components

### 1. Schema Definition (`db/tables.py`)

Single source of truth for the database schema. Uses **SQLAlchemy Core** `Table` objects (not ORM).

- **24 tables** defined as module-level `sa.Table(...)` objects
- Portable types: `sa.Integer`, `sa.Text`, `sa.Boolean`, `sa.DateTime`, `sa.Float`
- Foreign keys with `ondelete` cascading
- Indexes for performance-critical queries
- No FTS columns (handled by engine-specific plugins)

```python
from tune_server.db.tables import metadata, artists, albums, tracks
```

### 2. Database Engines

#### SQLite (`db/engine.py`)

- **Driver**: `aiosqlite` (async wrapper around sqlite3)
- **Connection**: Single connection with WAL mode
- **PRAGMAs**: `journal_mode=WAL`, `foreign_keys=ON`, `synchronous=NORMAL`
- **FTS**: FTS5 virtual tables with triggers for auto-sync
- **Backup**: Automatic file-based backups (keeps last 5)
- **Best for**: Development, small deployments, embedded server (iPadOS)

#### PostgreSQL (`db/postgres.py`)

- **Driver**: `asyncpg` (native async PostgreSQL driver)
- **Connection Pool**: `asyncpg.Pool` (configurable min=2, max=10)
- **FTS**: `tsvector` columns + GIN indexes + triggers
- **Backup**: User responsibility (`pg_dump`)
- **Best for**: Production, large libraries, multi-user

### 3. Database Factory (`db/factory.py`)

Selects the engine based on configuration:

```python
from tune_server.db.factory import create_database

db = create_database(config)  # Returns SQLiteDatabase or PostgresDatabase
```

Configuration via environment variables:
```bash
# SQLite (default)
TUNE_DB_ENGINE=sqlite
TUNE_DB_PATH=tune_server.db

# PostgreSQL
TUNE_DB_ENGINE=postgres
TUNE_DB_URL=postgresql://user:password@localhost/tune
TUNE_DB_POOL_MIN=2
TUNE_DB_POOL_MAX=10
```

### 4. DatabaseProtocol

Common interface implemented by all engines:

```python
class DatabaseProtocol(Protocol):
    engine_name: str
    
    async def connect() -> None
    async def close() -> None
    async def execute(sql, params) -> ExecuteResult
    async def executemany(sql, params_seq) -> None
    async def fetchone(sql, params) -> Row | None
    async def fetchall(sql, params) -> list[Row]
    async def commit() -> None
    async def executescript(sql) -> None
```

All repositories interact only with this protocol — they never import engine-specific code.

### 5. Repository Pattern (`db/repository.py`)

8 repositories encapsulating all database queries:

| Repository | Table(s) | Methods | Notes |
|-----------|----------|---------|-------|
| `ArtistRepo` | artists | 12 | FTS search, letter-based listing |
| `AlbumRepo` | albums, tracks (JOIN) | 15 | Quality detection, duplicate merging |
| `TrackRepo` | tracks, albums, artists | 19 | Directory traversal, deduplication |
| `PlayQueueRepo` | play_queue, tracks | 7 | Zone playback queue |
| `ZoneRepo` | zones | 5 | Zone CRUD |
| `PlaylistRepo` | playlists, playlist_tracks | 8 | Track ordering |
| `RadioStationRepo` | radio_stations | 7 | Favorite toggle |
| `RadioFavoriteRepo` | radio_favorites | 8 | Dedup by title+artist |

**Query patterns:**
- All queries are parameterized (`?` placeholders, auto-translated to `$N` for PG)
- Eager loading via JOINs (avoids N+1 queries)
- Engine-specific branches for FTS and string functions

### 6. Full-Text Search (FTS)

FTS is inherently engine-specific:

| Feature | SQLite | PostgreSQL |
|---------|--------|------------|
| Storage | FTS5 virtual tables | `tsvector` column |
| Index | Built-in | GIN index |
| Sync | Triggers (INSERT/DELETE/UPDATE) | Triggers |
| Query | `MATCH 'query*'` | `@@ plainto_tsquery('simple', 'query')` |
| Ranking | Built-in rank | `ts_rank()` |
| Diacritics | `remove_diacritics 2` | `'simple'` dictionary |

### 7. Schema Evolution

**Current system** (ad-hoc):
- `_run_migrations()` runs at each startup
- SQLite: `ALTER TABLE ADD COLUMN` wrapped in try/except
- PostgreSQL: `ADD COLUMN IF NOT EXISTS`
- No version tracking

**Planned** (Alembic):
- Versioned migrations in `db/alembic/versions/`
- `alembic upgrade head` at startup
- Rollback capability
- Auto-generate from `tables.py`

### 8. Migration Tool (`db/migrate.py`)

Transfers data between SQLite and PostgreSQL:

```bash
# SQLite → PostgreSQL
TUNE_DB_URL=postgresql://user:pass@localhost/tune \
python -m tune_server.db.migrate --from sqlite --to postgres

# PostgreSQL → SQLite
python -m tune_server.db.migrate --from postgres --to sqlite
```

Features:
- Respects foreign key dependency order (23 tables)
- Type conversion: timestamps (str ↔ datetime), booleans (int ↔ bool)
- Resets PostgreSQL sequences after migration
- Batch inserts (500 rows per batch)

## Tables

### Core Tables

```
artists ──< albums ──< tracks
                         │
playlists ──< playlist_tracks ──┘
                         │
zones ──< play_queue ────┘
```

### Streaming & Auth

- `streaming_auth` — OAuth tokens for Tidal, Qobuz, YouTube, Deezer, Spotify
- `radio_stations` — Internet radio stations
- `radio_favorites` — Saved radio track metadata

### User Management

- `user_profiles` — Named profiles with avatar color
- `user_favorites` — Track/album/artist favorites per user

### Metadata Management

- `metadata_suggestions` — AI-suggested metadata corrections
- `metadata_fix_reports` — Batch fix operation logs
- `duplicate_tracks` — Detected duplicates by audio hash

### Playlist Sync

- `playlist_links` — Links between local and streaming playlists
- `playlist_snapshots` — Point-in-time playlist state
- `sync_schedules` — Automated sync intervals
- `transfer_history` — Cross-service transfer logs

### Multi-Room

- `zone_groups` — Multi-room groups
- `zone_group_members` — Zone membership with volume offset
- `zone_profiles` — Named zone configurations

### Infrastructure

- `device_credentials` — DLNA/AirPlay device auth
- `network_mounts` — SMB/NFS mount points

## Performance Considerations

### Indexes

All foreign keys and frequently-queried columns are indexed:
- `artists.name`, `artists.sort_name`
- `albums.title`, `albums.artist_id`, `albums.year`, `albums.source+source_id`
- `tracks.album_id`, `tracks.artist_id`, `tracks.file_path`, `tracks.source+source_id`
- `play_queue.zone_id+position`
- `playlist_tracks.playlist_id+position`

### Connection Pooling (PostgreSQL)

Default pool: 2-10 connections. Tune via:
```bash
TUNE_DB_POOL_MIN=5
TUNE_DB_POOL_MAX=20
```

### WAL Mode (SQLite)

Write-Ahead Logging allows concurrent reads during writes. Critical for multi-client scenarios (web client + API + WebSocket).

## Roadmap

### Phase 1: SQLAlchemy Core Foundation ✅
- `tables.py` — single schema definition (24 tables)
- Dependencies added (sqlalchemy, alembic)

### Phase 2: SA Engine + FTS Plugin (planned)
- `sa_engine.py` — unified engine wrapping SA async
- `fts.py` — pluggable FTS (SQLite FTS5, PG tsvector, generic LIKE)

### Phase 3: Repository Migration (planned)
- Convert raw SQL → SA Core expressions in all 8 repos
- Extract 6 new repos from route files (~170 raw SQL calls)

### Phase 4: Alembic Integration (planned)
- Versioned migrations
- Auto-generate from schema changes
- Startup auto-upgrade

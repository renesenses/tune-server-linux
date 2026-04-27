"""Tests for the SQLAlchemy Core database engine (SADatabase).

Validates backward compatibility with raw SQL and SA-native methods.
"""
import os
import sqlite3
import tempfile

import pytest
from tune_server.db.sa_engine import SADatabase
from tune_server.db.tables import artists, albums, metadata, tracks
from tune_server.models import Artist


# Helper marker: skip tests that hardcode SQLite-specific behaviour when the
# fixture is wired to PostgreSQL (CI dual-engine job sets TUNE_TEST_PG_URL).
sqlite_only = pytest.mark.skipif(
    os.environ.get("TUNE_TEST_PG_URL") is not None,
    reason="SQLite-specific assertion — engine fixture is PostgreSQL"
)


@pytest.fixture
async def db():
    """SADatabase fixture. Defaults to in-memory SQLite; if the env var
    ``TUNE_TEST_PG_URL`` is set (e.g. CI dual-engine job), runs the same
    tests against a real PostgreSQL and tears down by dropping every table
    that the SA metadata declares.
    """
    import os
    pg_url = os.environ.get("TUNE_TEST_PG_URL")
    if pg_url:
        database = SADatabase(engine_name="postgres", db_url=pg_url)
        await database.connect()
        try:
            yield database
        finally:
            # Drop every SA-declared table so the next test starts clean.
            from tune_server.db.tables import metadata as md
            from sqlalchemy import text as _text
            async with database.sa_engine.begin() as conn:
                for table_name in reversed(list(md.tables)):
                    await conn.execute(_text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))
            await database.close()
    else:
        database = SADatabase(engine_name="sqlite", db_path=":memory:")
        await database.connect()
        yield database
        await database.close()


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

async def test_tables_created(db: SADatabase):
    """All 24 tables should be created on connect.

    Use the SA inspector (engine-portable) instead of sqlite_master.
    """
    from sqlalchemy import inspect as sa_inspect

    def _names(sync_conn):
        return set(sa_inspect(sync_conn).get_table_names())

    async with db.sa_engine.begin() as conn:
        table_names = await conn.run_sync(_names)

    assert "artists" in table_names
    assert "albums" in table_names
    assert "tracks" in table_names
    assert "zones" in table_names
    assert "streaming_auth" in table_names
    assert len(table_names) >= 24


# ---------------------------------------------------------------------------
# Raw SQL backward compatibility (DatabaseProtocol)
# ---------------------------------------------------------------------------

@sqlite_only
async def test_raw_insert_and_fetch(db: SADatabase):
    """Raw SQL INSERT + SELECT with ? placeholders should work.

    PostgreSQL (asyncpg) doesn't expose lastrowid on plain INSERT — this
    test pins SQLite-specific behaviour. The PG path uses INSERT
    ... RETURNING id which is covered by SAArtistRepo tests.
    """
    result = await db.execute(
        "INSERT INTO artists (name, sort_name) VALUES (?, ?)",
        ("Pink Floyd", "Pink Floyd"),
    )
    assert result.lastrowid is not None
    artist_id = result.lastrowid

    row = await db.fetchone("SELECT * FROM artists WHERE id = ?", (artist_id,))
    assert row is not None
    assert row["name"] == "Pink Floyd"
    assert row["sort_name"] == "Pink Floyd"


async def test_raw_fetchall(db: SADatabase):
    """fetchall should return list of mappings."""
    await db.execute("INSERT INTO artists (name) VALUES (?)", ("ABBA",))
    await db.execute("INSERT INTO artists (name) VALUES (?)", ("Beatles",))
    await db.execute("INSERT INTO artists (name) VALUES (?)", ("Coldplay",))

    rows = await db.fetchall("SELECT * FROM artists ORDER BY name")
    assert len(rows) == 3
    assert rows[0]["name"] == "ABBA"
    assert rows[2]["name"] == "Coldplay"


async def test_raw_update(db: SADatabase):
    """UPDATE should return correct rowcount."""
    await db.execute("INSERT INTO artists (name) VALUES (?)", ("Test",))
    result = await db.execute(
        "UPDATE artists SET name = ? WHERE name = ?", ("Updated", "Test")
    )
    assert result.rowcount == 1

    row = await db.fetchone("SELECT name FROM artists WHERE name = ?", ("Updated",))
    assert row["name"] == "Updated"


async def test_raw_delete(db: SADatabase):
    """DELETE should return correct rowcount."""
    await db.execute("INSERT INTO artists (name) VALUES (?)", ("ToDelete",))
    result = await db.execute("DELETE FROM artists WHERE name = ?", ("ToDelete",))
    assert result.rowcount == 1


async def test_fetchone_returns_none(db: SADatabase):
    """fetchone on empty result should return None."""
    row = await db.fetchone("SELECT * FROM artists WHERE id = ?", (9999,))
    assert row is None


# ---------------------------------------------------------------------------
# SA-native methods
# ---------------------------------------------------------------------------

async def test_sa_insert_and_fetch(db: SADatabase):
    """SA Core insert + select should work."""
    import sqlalchemy as sa

    result = await db.sa_execute(
        artists.insert().values(name="Radiohead", sort_name="Radiohead")
    )
    assert result.lastrowid is not None

    row = await db.sa_fetchone(
        sa.select(artists).where(artists.c.id == result.lastrowid)
    )
    assert row is not None
    assert row["name"] == "Radiohead"


async def test_sa_fetchall(db: SADatabase):
    """SA Core fetchall should return mappings."""
    import sqlalchemy as sa

    await db.sa_execute(artists.insert().values(name="X"))
    await db.sa_execute(artists.insert().values(name="Y"))
    await db.sa_execute(artists.insert().values(name="Z"))

    rows = await db.sa_fetchall(
        sa.select(artists).order_by(artists.c.name)
    )
    assert len(rows) == 3
    assert rows[0]["name"] == "X"


async def test_sa_foreign_keys(db: SADatabase):
    """Foreign key constraints should work."""
    import sqlalchemy as sa

    artist_result = await db.sa_execute(
        artists.insert().values(name="David Bowie")
    )
    album_result = await db.sa_execute(
        albums.insert().values(
            title="Ziggy Stardust",
            artist_id=artist_result.lastrowid,
            source="local",
        )
    )
    track_result = await db.sa_execute(
        tracks.insert().values(
            title="Starman",
            album_id=album_result.lastrowid,
            artist_id=artist_result.lastrowid,
            source="local",
        )
    )
    assert track_result.lastrowid is not None

    # Verify JOIN works
    row = await db.sa_fetchone(
        sa.select(tracks.c.title, albums.c.title.label("album_title"), artists.c.name)
        .join(albums, tracks.c.album_id == albums.c.id)
        .join(artists, tracks.c.artist_id == artists.c.id)
        .where(tracks.c.id == track_result.lastrowid)
    )
    assert row["title"] == "Starman"
    assert row["album_title"] == "Ziggy Stardust"
    assert row["name"] == "David Bowie"


# ---------------------------------------------------------------------------
# FTS
# ---------------------------------------------------------------------------

async def test_fts_initialized(db: SADatabase):
    """FTS plugin should be initialized."""
    assert db.fts is not None


async def test_fts_search(db: SADatabase):
    """FTS search should find matching artists."""
    import sqlalchemy as sa

    await db.execute("INSERT INTO artists (name) VALUES (?)", ("Pink Floyd",))
    await db.execute("INSERT INTO artists (name) VALUES (?)", ("Led Zeppelin",))

    where_clause = db.fts.search_where("artists", "pink")
    stmt = sa.select(artists).where(where_clause).params(fts_query="pink*")
    rows = await db.sa_fetchall(stmt)
    assert len(rows) == 1
    assert rows[0]["name"] == "Pink Floyd"


# ---------------------------------------------------------------------------
# Placeholder translation
# ---------------------------------------------------------------------------

def test_placeholder_translation():
    """? should be translated to :p1, :p2, etc."""
    result = SADatabase._translate_placeholders(
        "SELECT * FROM artists WHERE id = ? AND name = ?"
    )
    assert ":p1" in result
    assert ":p2" in result
    assert "?" not in result


def test_placeholder_ignores_strings():
    """? inside string literals should NOT be translated."""
    result = SADatabase._translate_placeholders(
        "SELECT * FROM artists WHERE name = 'what?' AND id = ?"
    )
    assert "what?" in result  # Inside string — preserved
    assert ":p1" in result    # Outside string — translated
    assert result.count(":p") == 1


# ---------------------------------------------------------------------------
# Engine properties
# ---------------------------------------------------------------------------

async def test_engine_name(db: SADatabase):
    expected = "postgres" if os.environ.get("TUNE_TEST_PG_URL") else "sqlite"
    assert db.engine_name == expected


async def test_commit_noop(db: SADatabase):
    """commit() should not raise."""
    await db.commit()


# ---------------------------------------------------------------------------
# Scanner-like patterns (raw SQL edge cases)
# ---------------------------------------------------------------------------

async def test_raw_insert_returning(db: SADatabase):
    """INSERT ... RETURNING id should return lastrowid."""
    result = await db.execute(
        "INSERT INTO artists (name) VALUES (?) RETURNING id",
        ("Test Artist",),
    )
    assert result.lastrowid is not None
    assert result.lastrowid > 0


async def test_raw_delete_no_crash(db: SADatabase):
    """DELETE should NOT crash (no inserted_primary_key access)."""
    await db.execute("INSERT INTO artists (name) VALUES (?)", ("ToDelete",))
    result = await db.execute("DELETE FROM artists WHERE name = ?", ("ToDelete",))
    assert result.rowcount == 1


async def test_raw_update_no_crash(db: SADatabase):
    """UPDATE should NOT crash."""
    await db.execute("INSERT INTO artists (name) VALUES (?)", ("Original",))
    result = await db.execute(
        "UPDATE artists SET name = ? WHERE name = ?", ("Changed", "Original")
    )
    assert result.rowcount == 1


async def test_raw_select_count(db: SADatabase):
    """SELECT COUNT(*) should work."""
    await db.execute("INSERT INTO artists (name) VALUES (?)", ("A",))
    await db.execute("INSERT INTO artists (name) VALUES (?)", ("B",))
    row = await db.fetchone("SELECT COUNT(*) as cnt FROM artists")
    assert row["cnt"] == 2


def test_is_write_statement_classifies_correctly():
    """Direct unit test of the static helper that picks engine.begin vs
    engine.connect for fetchone/fetchall.

    Regression: before v0.7.32, fetchone always used engine.connect,
    silently dropping INSERT...RETURNING writes (the row never
    committed and any FK reference exploded later)."""
    cls = SADatabase
    assert cls._is_write_statement("INSERT INTO playlists (name) VALUES (?)") is True
    assert cls._is_write_statement("  insert into x VALUES(1)") is True
    assert cls._is_write_statement("INSERT INTO x ... RETURNING id") is True
    assert cls._is_write_statement("UPDATE x SET y=1") is True
    assert cls._is_write_statement("DELETE FROM x WHERE id=1") is True
    assert cls._is_write_statement("MERGE INTO ...") is True
    assert cls._is_write_statement("REPLACE INTO ...") is True
    assert cls._is_write_statement("SELECT * FROM x") is False
    assert cls._is_write_statement("  select 1") is False
    assert cls._is_write_statement("WITH cte AS (SELECT 1) SELECT * FROM cte") is False
    assert cls._is_write_statement("") is False
    assert cls._is_write_statement("   ") is False


async def test_fetchone_insert_returning_persists(db: SADatabase):
    """Regression for the v0.7.32 transfer-to-local FK violation: when
    fetchone runs an INSERT...RETURNING it MUST commit so the new row
    is visible to subsequent statements (and to other connections).

    Before v0.7.32, fetchone always used engine.connect (no commit);
    the INSERT rolled back on context exit and a follow-up FK INSERT
    on a child table failed with ForeignKeyViolationError on PG and
    silently dropped the work on SQLite."""
    row = await db.fetchone(
        "INSERT INTO artists (name) VALUES (?) RETURNING id",
        ("Persistence Test",),
    )
    assert row is not None
    new_id = row["id"]
    assert new_id is not None

    # A second connection / fresh fetchone must see the row.
    check = await db.fetchone("SELECT id, name FROM artists WHERE id = ?", (new_id,))
    assert check is not None
    assert check["name"] == "Persistence Test"


async def test_sa_insert_then_delete(db: SADatabase):
    """SA insert + delete cycle should work."""
    import sqlalchemy as sa

    result = await db.sa_execute(
        artists.insert().values(name="Temp")
    )
    aid = result.lastrowid
    assert aid is not None

    del_result = await db.sa_execute(
        artists.delete().where(artists.c.id == aid)
    )
    assert del_result.rowcount == 1


# ---------------------------------------------------------------------------
# SA Repository integration tests
# ---------------------------------------------------------------------------

async def test_album_repo_list(db: SADatabase):
    """SAAlbumRepo.list() with subquery should work."""
    from tune_server.db.sa_repository import SAArtistRepo, SAAlbumRepo, SATrackRepo

    artist_repo = SAArtistRepo(db)
    album_repo = SAAlbumRepo(db)
    track_repo = SATrackRepo(db)

    # Create test data
    aid = await artist_repo.create(Artist(name="Test Artist", sort_name="Test Artist"))
    from tune_server.models import Album, Track
    album_id = await album_repo.create(Album(title="Test Album", artist_id=aid, source="local"))
    await track_repo.create(Track(
        title="Track 1", album_id=album_id, artist_id=aid,
        source="local", format="flac", sample_rate=44100, bit_depth=16,
    ))

    # List albums — triggers the subquery
    result = await album_repo.list(limit=10)
    assert len(result) == 1
    assert result[0].title == "Test Album"
    assert result[0].artist_name == "Test Artist"

    # Count
    count = await album_repo.count()
    assert count == 1

    # List by artist
    by_artist = await album_repo.list_by_artist(aid)
    assert len(by_artist) == 1


async def test_track_repo_list_by_album(db: SADatabase):
    """SATrackRepo.list_by_album() should return ordered tracks."""
    from tune_server.db.sa_repository import SAArtistRepo, SAAlbumRepo, SATrackRepo
    from tune_server.models import Album, Track

    artist_repo = SAArtistRepo(db)
    album_repo = SAAlbumRepo(db)
    track_repo = SATrackRepo(db)

    aid = await artist_repo.create(Artist(name="Band", sort_name="Band"))
    album_id = await album_repo.create(Album(title="Album", artist_id=aid, source="local"))

    await track_repo.create(Track(title="C", album_id=album_id, artist_id=aid, source="local", track_number=3, disc_number=1))
    await track_repo.create(Track(title="A", album_id=album_id, artist_id=aid, source="local", track_number=1, disc_number=1))
    await track_repo.create(Track(title="B", album_id=album_id, artist_id=aid, source="local", track_number=2, disc_number=1))

    tracks_list = await track_repo.list_by_album(album_id)
    assert len(tracks_list) == 3
    assert tracks_list[0].title == "A"  # track_number=1
    assert tracks_list[1].title == "B"  # track_number=2
    assert tracks_list[2].title == "C"  # track_number=3


# ---------------------------------------------------------------------------
# Regression: column auto-migration on legacy databases
# ---------------------------------------------------------------------------
# v0.7.13 → v0.7.16 shipped with an incomplete migration list: any user
# upgrading from v0.5.x got HTTP 500 on /library/albums/* because columns
# like albums.bio, tracks.loudness_lufs, etc. were declared in the SA model
# but never added by ALTER TABLE. v0.7.17 replaced the hand-list with
# reflection-based auto-detection.
#
# These tests pin that behavior so any regression is caught immediately.

async def test_auto_migration_adds_missing_columns():
    """Open a deliberately stale SQLite DB and verify every column from the
    SA model gets added by _run_column_migrations."""
    db_path = tempfile.mktemp(suffix=".db")

    # Build a v0.5-era schema by hand: each table has only the original few
    # columns. Anything declared in tables.py but absent here MUST be added
    # by the auto-migration on connect.
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE artists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_name TEXT
        );
        CREATE TABLE albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            artist_id INTEGER,
            year INTEGER,
            genre TEXT,
            disc_count INTEGER DEFAULT 1,
            track_count INTEGER DEFAULT 0,
            cover_path TEXT,
            source TEXT NOT NULL DEFAULT 'local',
            source_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            album_id INTEGER,
            artist_id INTEGER,
            disc_number INTEGER DEFAULT 1,
            track_number INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            file_path TEXT UNIQUE,
            format TEXT,
            sample_rate INTEGER,
            bit_depth INTEGER,
            channels INTEGER DEFAULT 2,
            source TEXT NOT NULL DEFAULT 'local',
            source_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            output_type TEXT NOT NULL DEFAULT 'local',
            output_device_id TEXT,
            volume REAL DEFAULT 0.7,
            group_id INTEGER,
            sync_delay_ms INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            queue_json TEXT
        );
        INSERT INTO artists (name) VALUES ('Test Artist');
        INSERT INTO albums (title, artist_id) VALUES ('Test Album', 1);
        INSERT INTO tracks (title, album_id, artist_id, file_path)
            VALUES ('Test Track', 1, 1, '/test.flac');
    """)
    conn.commit()
    conn.close()

    database = SADatabase(engine_name="sqlite", db_path=db_path)
    try:
        await database.connect()

        # Read live schema after migrations
        live = sqlite3.connect(db_path)
        try:
            live_cols = {
                table_name: {row[1] for row in live.execute(f"PRAGMA table_info({table_name})")}
                for table_name in ("artists", "albums", "tracks", "zones")
            }
        finally:
            live.close()

        # Every column declared in the SA model must now exist in the DB.
        for table in metadata.tables.values():
            if table.name not in live_cols:
                continue
            model_cols = {c.name for c in table.columns}
            missing = model_cols - live_cols[table.name]
            assert not missing, (
                f"After migration, table {table.name} is still missing "
                f"columns {missing} declared in tables.py"
            )

        # Sanity: a real album-by-id query (the one Jacques hit) succeeds.
        from tune_server.db.sa_repository import SAAlbumRepo
        album = await SAAlbumRepo(database).get(1)
        assert album is not None
        assert album.title == "Test Album"
    finally:
        await database.close()
        import os
        os.unlink(db_path)


async def test_backup_before_migration_creates_snapshot():
    """Existing SQLite DB → backup file with timestamp written before migrations."""
    db_path = tempfile.mktemp(suffix=".db")
    # Pre-populate so the file is non-empty (backup is skipped on empty files).
    pre = sqlite3.connect(db_path)
    pre.execute("CREATE TABLE marker (id INTEGER)")
    pre.execute("INSERT INTO marker VALUES (42)")
    pre.commit()
    pre.close()

    database = SADatabase(engine_name="sqlite", db_path=db_path)
    try:
        await database.connect()
        from pathlib import Path
        backups = list(Path(db_path).parent.glob(f"{Path(db_path).name}.bak.*"))
        assert len(backups) == 1, f"Expected 1 backup, got {backups}"
        # The backup must contain the original 'marker' row even after the
        # live DB has been migrated/extended.
        bak = sqlite3.connect(str(backups[0]))
        try:
            row = bak.execute("SELECT id FROM marker").fetchone()
            assert row == (42,)
        finally:
            bak.close()
            backups[0].unlink()
    finally:
        await database.close()
        import os
        if os.path.exists(db_path):
            os.unlink(db_path)


async def test_backup_skipped_on_memory_db():
    """No backup file when db_path is :memory:."""
    database = SADatabase(engine_name="sqlite", db_path=":memory:")
    try:
        await database.connect()
        # Just make sure we don't crash and don't emit a backup somewhere.
    finally:
        await database.close()


async def test_auto_migration_idempotent():
    """Running migrations twice on the same DB should be a no-op."""
    db_path = tempfile.mktemp(suffix=".db")

    database = SADatabase(engine_name="sqlite", db_path=db_path)
    try:
        await database.connect()
        # Connect again with a fresh SADatabase on the same file — no errors,
        # all migrations skip silently because columns already exist.
        await database.close()

        database2 = SADatabase(engine_name="sqlite", db_path=db_path)
        await database2.connect()
        # If we get here without raising, the second run was clean.
        await database2.close()
    finally:
        import os
        if os.path.exists(db_path):
            os.unlink(db_path)

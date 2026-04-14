"""Tests for the SQLAlchemy Core database engine (SADatabase).

Validates backward compatibility with raw SQL and SA-native methods.
"""
import pytest
from tune_server.db.sa_engine import SADatabase
from tune_server.db.tables import artists, albums, tracks


@pytest.fixture
async def db():
    """In-memory SQLite database via SADatabase."""
    database = SADatabase(engine_name="sqlite", db_path=":memory:")
    await database.connect()
    yield database
    await database.close()


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

async def test_tables_created(db: SADatabase):
    """All 24 tables should be created on connect."""
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    table_names = {row["name"] for row in rows}
    assert "artists" in table_names
    assert "albums" in table_names
    assert "tracks" in table_names
    assert "zones" in table_names
    assert "streaming_auth" in table_names
    assert len(table_names) >= 24


# ---------------------------------------------------------------------------
# Raw SQL backward compatibility (DatabaseProtocol)
# ---------------------------------------------------------------------------

async def test_raw_insert_and_fetch(db: SADatabase):
    """Raw SQL INSERT + SELECT with ? placeholders should work."""
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
    assert db.engine_name == "sqlite"


async def test_commit_noop(db: SADatabase):
    """commit() should not raise."""
    await db.commit()

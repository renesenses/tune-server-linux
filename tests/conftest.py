from __future__ import annotations

from unittest.mock import AsyncMock, PropertyMock

import pytest

from tune_server.audio.formats import LOCAL_CAPABILITIES, AudioCapabilities
from tune_server.db.engine import Database
from tune_server.db.repository import AlbumRepo, ArtistRepo, PlaylistRepo, PlayQueueRepo, TrackRepo, ZoneRepo
from tune_server.event_bus import EventBus
from tune_server.models import Album, Artist, AudioFormat, OutputType, Source, Track


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


# ---------------------------------------------------------------------------
# Repositories (need db)
# ---------------------------------------------------------------------------

@pytest.fixture
def artist_repo(db):
    return ArtistRepo(db)


@pytest.fixture
def album_repo(db):
    return AlbumRepo(db)


@pytest.fixture
def track_repo(db):
    return TrackRepo(db)


@pytest.fixture
def queue_repo(db):
    return PlayQueueRepo(db)


@pytest.fixture
def playlist_repo(db):
    return PlaylistRepo(db)


@pytest.fixture
def zone_repo(db):
    return ZoneRepo(db)


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

@pytest.fixture
def event_bus():
    return EventBus()


# ---------------------------------------------------------------------------
# Sample data — pre-inserted into the DB
# ---------------------------------------------------------------------------

@pytest.fixture
async def sample_artist(artist_repo) -> Artist:
    artist_id = await artist_repo.create(
        Artist(name="Alain Bashung", sort_name="Bashung, Alain")
    )
    return Artist(id=artist_id, name="Alain Bashung", sort_name="Bashung, Alain")


@pytest.fixture
async def sample_album(album_repo, sample_artist) -> Album:
    album_id = await album_repo.create(
        Album(
            title="Fantaisie Militaire",
            artist_id=sample_artist.id,
            year=1998,
            genre="Chanson",
            source=Source.LOCAL,
        )
    )
    return Album(
        id=album_id,
        title="Fantaisie Militaire",
        artist_id=sample_artist.id,
        artist_name=sample_artist.name,
        year=1998,
        genre="Chanson",
        source=Source.LOCAL,
    )


@pytest.fixture
async def sample_tracks(track_repo, sample_album, sample_artist) -> list[Track]:
    tracks = []
    for i, title in enumerate(
        ["La nuit je mens", "Aucun express", "Fantaisie militaire"], start=1
    ):
        track = Track(
            title=title,
            album_id=sample_album.id,
            artist_id=sample_artist.id,
            disc_number=1,
            track_number=i,
            duration_ms=240_000 + i * 10_000,
            file_path=f"/music/bashung/fantaisie/{i:02d}_{title.lower().replace(' ', '_')}.flac",
            format=AudioFormat.FLAC,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
            source=Source.LOCAL,
        )
        track_id = await track_repo.create(track)
        track.id = track_id
        track.album_title = sample_album.title
        track.artist_name = sample_artist.name
        tracks.append(track)
    return tracks


# ---------------------------------------------------------------------------
# Mock output target
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_output():
    output = AsyncMock()
    output.name = "mock-output"
    type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
    type(output).is_available = PropertyMock(return_value=True)
    return output


# ---------------------------------------------------------------------------
# FastAPI test client
# ---------------------------------------------------------------------------

@pytest.fixture
async def app_client(db, sample_artist, sample_album, sample_tracks, event_bus):
    import httpx
    from tune_server.api.deps import deps
    from tune_server.api.main import create_api_app

    # Wire up deps
    deps.db = db
    deps.event_bus = event_bus
    deps.track_repo = TrackRepo(db)
    deps.album_repo = AlbumRepo(db)
    deps.artist_repo = ArtistRepo(db)
    deps.playlist_repo = PlaylistRepo(db)
    deps.queue_repo = PlayQueueRepo(db)
    deps.zone_repo = ZoneRepo(db)

    app = create_api_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client

    # Clean up global state
    deps.db = None
    deps.event_bus = None
    deps.track_repo = None
    deps.album_repo = None
    deps.artist_repo = None
    deps.playlist_repo = None
    deps.queue_repo = None
    deps.zone_repo = None


# ---------------------------------------------------------------------------
# Zone-related fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def zone_instance(mock_output, event_bus):
    from tune_server.models import OutputType
    from tune_server.zones.zone import ZoneInstance

    return ZoneInstance(
        zone_id=1,
        name="Test Zone",
        output_type=OutputType.LOCAL,
        output=mock_output,
        event_bus=event_bus,
    )


@pytest.fixture
async def zone_manager(db, event_bus):
    from tune_server.zones.manager import ZoneManager

    zm = ZoneManager(db, event_bus)

    async def _mock_local_factory(device_id):
        output = AsyncMock()
        output.name = "mock-local"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        return output

    zm.register_output_factory(OutputType.LOCAL, _mock_local_factory)
    return zm


@pytest.fixture
def group_manager(event_bus):
    from tune_server.zones.group import GroupManager

    return GroupManager(event_bus)


@pytest.fixture
async def app_client_with_zones(db, sample_artist, sample_album, sample_tracks, event_bus):
    import httpx
    from tune_server.api.deps import deps
    from tune_server.api.main import create_api_app
    from tune_server.zones.group import GroupManager
    from tune_server.zones.manager import ZoneManager

    # Wire up deps
    deps.db = db
    deps.event_bus = event_bus
    deps.track_repo = TrackRepo(db)
    deps.album_repo = AlbumRepo(db)
    deps.artist_repo = ArtistRepo(db)
    deps.queue_repo = PlayQueueRepo(db)
    deps.zone_repo = ZoneRepo(db)

    zm = ZoneManager(db, event_bus)

    async def _mock_local_factory(device_id):
        output = AsyncMock()
        output.name = "mock-local"
        type(output).capabilities = PropertyMock(return_value=LOCAL_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)
        return output

    zm.register_output_factory(OutputType.LOCAL, _mock_local_factory)
    deps.zone_manager = zm
    deps.group_manager = GroupManager(event_bus)

    app = create_api_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client

    # Clean up global state
    deps.db = None
    deps.event_bus = None
    deps.track_repo = None
    deps.album_repo = None
    deps.artist_repo = None
    deps.queue_repo = None
    deps.zone_repo = None
    deps.zone_manager = None
    deps.group_manager = None

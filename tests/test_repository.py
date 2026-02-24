from __future__ import annotations

import pytest

from tune_server.db.repository import (
    AlbumRepo,
    ArtistRepo,
    PlayQueueRepo,
    TrackRepo,
    ZoneRepo,
    full_text_search,
)
from tune_server.models import Album, Artist, AudioFormat, Source, Track


# -----------------------------------------------------------------------
# ArtistRepo
# -----------------------------------------------------------------------

class TestArtistRepo:
    async def test_create_and_get(self, artist_repo: ArtistRepo):
        artist_id = await artist_repo.create(
            Artist(name="Noir Désir", sort_name="Noir Désir")
        )
        artist = await artist_repo.get(artist_id)
        assert artist is not None
        assert artist.id == artist_id
        assert artist.name == "Noir Désir"
        assert artist.sort_name == "Noir Désir"

    async def test_get_nonexistent(self, artist_repo: ArtistRepo):
        assert await artist_repo.get(9999) is None

    async def test_get_or_create_new(self, artist_repo: ArtistRepo):
        artist = await artist_repo.get_or_create("Serge Gainsbourg")
        assert artist.id is not None
        assert artist.name == "Serge Gainsbourg"

    async def test_get_or_create_existing(self, artist_repo: ArtistRepo):
        a1 = await artist_repo.get_or_create("Jacques Brel")
        a2 = await artist_repo.get_or_create("Jacques Brel")
        assert a1.id == a2.id

    async def test_list_pagination(self, artist_repo: ArtistRepo):
        for name in ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]:
            await artist_repo.create(Artist(name=name, sort_name=name))

        page1 = await artist_repo.list(limit=2, offset=0)
        page2 = await artist_repo.list(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].name != page2[0].name

    async def test_count(self, artist_repo: ArtistRepo):
        assert await artist_repo.count() == 0
        await artist_repo.create(Artist(name="Test"))
        assert await artist_repo.count() == 1

    async def test_delete(self, artist_repo: ArtistRepo):
        aid = await artist_repo.create(Artist(name="To Delete"))
        await artist_repo.delete(aid)
        assert await artist_repo.get(aid) is None

    async def test_search_fts(self, artist_repo: ArtistRepo):
        await artist_repo.create(
            Artist(name="Alain Bashung", sort_name="Bashung, Alain")
        )
        await artist_repo.create(Artist(name="Alain Souchon"))

        results = await artist_repo.search("Bash")
        assert len(results) == 1
        assert results[0].name == "Alain Bashung"

    async def test_update(self, artist_repo: ArtistRepo):
        aid = await artist_repo.create(Artist(name="Old Name"))
        artist = await artist_repo.get(aid)
        artist.name = "New Name"
        artist.bio = "Updated bio"
        await artist_repo.update(artist)
        updated = await artist_repo.get(aid)
        assert updated.name == "New Name"
        assert updated.bio == "Updated bio"


# -----------------------------------------------------------------------
# AlbumRepo
# -----------------------------------------------------------------------

class TestAlbumRepo:
    async def test_create_and_get(self, album_repo: AlbumRepo, sample_artist: Artist):
        album_id = await album_repo.create(
            Album(
                title="Bleu Pétrole",
                artist_id=sample_artist.id,
                year=2008,
                genre="Chanson",
            )
        )
        album = await album_repo.get(album_id)
        assert album is not None
        assert album.title == "Bleu Pétrole"
        assert album.artist_name == sample_artist.name

    async def test_list_by_artist(self, album_repo: AlbumRepo, sample_artist: Artist):
        await album_repo.create(Album(title="Album A", artist_id=sample_artist.id, year=1990))
        await album_repo.create(Album(title="Album B", artist_id=sample_artist.id, year=2000))

        albums = await album_repo.list_by_artist(sample_artist.id)
        assert len(albums) == 2
        # Ordered by year
        assert albums[0].year == 1990
        assert albums[1].year == 2000

    async def test_get_or_create(self, album_repo: AlbumRepo, sample_artist: Artist):
        a1 = await album_repo.get_or_create("Test Album", sample_artist.id)
        a2 = await album_repo.get_or_create("Test Album", sample_artist.id)
        assert a1.id == a2.id

    async def test_update_track_count(
        self, db, album_repo: AlbumRepo, track_repo: TrackRepo, sample_artist: Artist
    ):
        album_id = await album_repo.create(
            Album(title="Counted", artist_id=sample_artist.id)
        )
        await track_repo.create(
            Track(title="T1", album_id=album_id, artist_id=sample_artist.id)
        )
        await track_repo.create(
            Track(title="T2", album_id=album_id, artist_id=sample_artist.id)
        )
        await album_repo.update_track_count(album_id)

        album = await album_repo.get(album_id)
        assert album.track_count == 2

    async def test_search_fts(self, album_repo: AlbumRepo, sample_artist: Artist):
        await album_repo.create(
            Album(title="Fantaisie Militaire", artist_id=sample_artist.id)
        )
        await album_repo.create(
            Album(title="Bleu Pétrole", artist_id=sample_artist.id)
        )

        results = await album_repo.search("Fantaisie")
        assert len(results) == 1
        assert results[0].title == "Fantaisie Militaire"

    async def test_count(self, album_repo: AlbumRepo):
        assert await album_repo.count() == 0


# -----------------------------------------------------------------------
# TrackRepo
# -----------------------------------------------------------------------

class TestTrackRepo:
    async def test_create_and_get(
        self, track_repo: TrackRepo, sample_album: Album, sample_artist: Artist
    ):
        tid = await track_repo.create(
            Track(
                title="La nuit je mens",
                album_id=sample_album.id,
                artist_id=sample_artist.id,
                disc_number=1,
                track_number=1,
                duration_ms=300000,
                file_path="/music/la_nuit.flac",
                format=AudioFormat.FLAC,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            )
        )
        track = await track_repo.get(tid)
        assert track is not None
        assert track.title == "La nuit je mens"
        assert track.album_title == sample_album.title
        assert track.artist_name == sample_artist.name

    async def test_get_by_path(
        self, track_repo: TrackRepo, sample_album: Album, sample_artist: Artist
    ):
        await track_repo.create(
            Track(
                title="Some Track",
                album_id=sample_album.id,
                artist_id=sample_artist.id,
                file_path="/unique/path.flac",
            )
        )
        track = await track_repo.get_by_path("/unique/path.flac")
        assert track is not None
        assert track.title == "Some Track"

    async def test_list_by_album(self, track_repo: TrackRepo, sample_album, sample_artist):
        await track_repo.create(
            Track(
                title="T2",
                album_id=sample_album.id,
                artist_id=sample_artist.id,
                disc_number=1,
                track_number=2,
                file_path="/music/t2.flac",
            )
        )
        await track_repo.create(
            Track(
                title="T1",
                album_id=sample_album.id,
                artist_id=sample_artist.id,
                disc_number=1,
                track_number=1,
                file_path="/music/t1.flac",
            )
        )
        tracks = await track_repo.list_by_album(sample_album.id)
        assert len(tracks) == 2
        # Ordered by disc_number, track_number
        assert tracks[0].track_number == 1
        assert tracks[1].track_number == 2

    async def test_get_all_paths(self, track_repo: TrackRepo, sample_album, sample_artist):
        await track_repo.create(
            Track(
                title="Path A",
                album_id=sample_album.id,
                artist_id=sample_artist.id,
                file_path="/music/a.flac",
                source=Source.LOCAL,
            )
        )
        await track_repo.create(
            Track(
                title="Path B",
                album_id=sample_album.id,
                artist_id=sample_artist.id,
                file_path="/music/b.flac",
                source=Source.LOCAL,
            )
        )
        paths = await track_repo.get_all_paths()
        assert paths == {"/music/a.flac", "/music/b.flac"}

    async def test_get_multiple(self, track_repo: TrackRepo, sample_album, sample_artist):
        id1 = await track_repo.create(
            Track(title="Multi A", album_id=sample_album.id, artist_id=sample_artist.id, file_path="/m/a.flac")
        )
        id2 = await track_repo.create(
            Track(title="Multi B", album_id=sample_album.id, artist_id=sample_artist.id, file_path="/m/b.flac")
        )
        tracks = await track_repo.get_multiple([id1, id2])
        assert len(tracks) == 2

    async def test_get_multiple_empty(self, track_repo: TrackRepo):
        assert await track_repo.get_multiple([]) == []

    async def test_search_fts(self, track_repo: TrackRepo, sample_album, sample_artist):
        await track_repo.create(
            Track(
                title="La nuit je mens",
                album_id=sample_album.id,
                artist_id=sample_artist.id,
                file_path="/fts/nuit.flac",
            )
        )
        await track_repo.create(
            Track(
                title="Aucun express",
                album_id=sample_album.id,
                artist_id=sample_artist.id,
                file_path="/fts/aucun.flac",
            )
        )
        results = await track_repo.search("nuit")
        assert len(results) == 1
        assert results[0].title == "La nuit je mens"

    async def test_delete_by_path(self, track_repo: TrackRepo, sample_album, sample_artist):
        await track_repo.create(
            Track(
                title="Delete Me",
                album_id=sample_album.id,
                artist_id=sample_artist.id,
                file_path="/delete/me.flac",
            )
        )
        assert await track_repo.get_by_path("/delete/me.flac") is not None
        await track_repo.delete_by_path("/delete/me.flac")
        assert await track_repo.get_by_path("/delete/me.flac") is None

    async def test_count(self, track_repo: TrackRepo):
        assert await track_repo.count() == 0


# -----------------------------------------------------------------------
# ZoneRepo
# -----------------------------------------------------------------------

class TestZoneRepo:
    async def test_create_and_get(self, zone_repo: ZoneRepo):
        zid = await zone_repo.create("Salon", "local")
        zone = await zone_repo.get(zid)
        assert zone is not None
        assert zone["name"] == "Salon"
        assert zone["output_type"] == "local"

    async def test_list(self, zone_repo: ZoneRepo):
        await zone_repo.create("A", "local")
        await zone_repo.create("B", "dlna")
        zones = await zone_repo.list()
        assert len(zones) == 2

    async def test_delete_cascades_queue(self, zone_repo: ZoneRepo, queue_repo: PlayQueueRepo, track_repo, sample_album, sample_artist):
        zid = await zone_repo.create("Temp", "local")
        tid = await track_repo.create(
            Track(title="Q Track", album_id=sample_album.id, artist_id=sample_artist.id, file_path="/q.flac")
        )
        await queue_repo.set_queue(zid, [tid])
        assert await queue_repo.count(zid) == 1

        await zone_repo.delete(zid)
        assert await zone_repo.get(zid) is None
        assert await queue_repo.count(zid) == 0


# -----------------------------------------------------------------------
# full_text_search
# -----------------------------------------------------------------------

class TestFullTextSearch:
    async def test_full_text_search(self, db, sample_artist, sample_album, sample_tracks):
        result = await full_text_search(db, "nuit")
        # "La nuit je mens" should be in tracks
        assert len(result.tracks) >= 1
        assert any("nuit" in t.title.lower() for t in result.tracks)

    async def test_search_artist(self, db, sample_artist, sample_album, sample_tracks):
        result = await full_text_search(db, "Bashung")
        assert len(result.artists) >= 1
        assert result.artists[0].name == "Alain Bashung"

    async def test_search_album(self, db, sample_artist, sample_album, sample_tracks):
        result = await full_text_search(db, "Fantaisie")
        assert len(result.albums) >= 1
        assert result.albums[0].title == "Fantaisie Militaire"

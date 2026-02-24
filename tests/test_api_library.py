from __future__ import annotations

import os
import tempfile

import pytest


class TestRoot:
    async def test_root_endpoint(self, app_client):
        resp = await app_client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Tune Server"
        assert data["version"] == "0.1.0"
        assert "api" in data


class TestHealth:
    async def test_health(self, app_client):
        resp = await app_client.get("/api/v1/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded")
        assert "components" in data


class TestTracks:
    async def test_list_tracks(self, app_client):
        resp = await app_client.get("/api/v1/library/tracks")
        assert resp.status_code == 200
        tracks = resp.json()
        assert isinstance(tracks, list)
        assert len(tracks) == 3  # sample_tracks has 3

    async def test_list_tracks_pagination(self, app_client):
        resp = await app_client.get("/api/v1/library/tracks", params={"limit": 2, "offset": 1})
        assert resp.status_code == 200
        tracks = resp.json()
        assert len(tracks) == 2

    async def test_get_track(self, app_client):
        resp = await app_client.get("/api/v1/library/tracks/1")
        assert resp.status_code == 200
        track = resp.json()
        assert track["id"] == 1
        assert "title" in track

    async def test_get_track_404(self, app_client):
        resp = await app_client.get("/api/v1/library/tracks/9999")
        assert resp.status_code == 404


class TestAlbums:
    async def test_list_albums(self, app_client):
        resp = await app_client.get("/api/v1/library/albums")
        assert resp.status_code == 200
        albums = resp.json()
        assert isinstance(albums, list)
        assert len(albums) == 1  # sample_album

    async def test_get_album(self, app_client):
        resp = await app_client.get("/api/v1/library/albums/1")
        assert resp.status_code == 200
        album = resp.json()
        assert album["title"] == "Fantaisie Militaire"
        assert album["artist_name"] == "Alain Bashung"

    async def test_get_album_tracks(self, app_client):
        resp = await app_client.get("/api/v1/library/albums/1/tracks")
        assert resp.status_code == 200
        tracks = resp.json()
        assert len(tracks) == 3
        # Ordered by disc_number, track_number
        assert tracks[0]["track_number"] <= tracks[1]["track_number"]


class TestArtists:
    async def test_list_artists(self, app_client):
        resp = await app_client.get("/api/v1/library/artists")
        assert resp.status_code == 200
        artists = resp.json()
        assert isinstance(artists, list)
        assert len(artists) == 1

    async def test_get_artist(self, app_client):
        resp = await app_client.get("/api/v1/library/artists/1")
        assert resp.status_code == 200
        artist = resp.json()
        assert artist["name"] == "Alain Bashung"

    async def test_get_artist_albums(self, app_client):
        resp = await app_client.get("/api/v1/library/artists/1/albums")
        assert resp.status_code == 200
        albums = resp.json()
        assert len(albums) == 1
        assert albums[0]["title"] == "Fantaisie Militaire"


class TestSearch:
    async def test_search(self, app_client):
        resp = await app_client.get("/api/v1/library/search", params={"q": "nuit"})
        assert resp.status_code == 200
        data = resp.json()
        assert "tracks" in data
        assert "albums" in data
        assert "artists" in data
        assert len(data["tracks"]) >= 1

    async def test_search_no_query(self, app_client):
        resp = await app_client.get("/api/v1/library/search")
        assert resp.status_code == 422  # missing required param


class TestStats:
    async def test_library_stats(self, app_client):
        resp = await app_client.get("/api/v1/library/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tracks"] == 3
        assert data["albums"] == 1
        assert data["artists"] == 1


class TestTrackEditing:
    async def test_update_track(self, app_client):
        resp = await app_client.put(
            "/api/v1/library/tracks/1",
            json={"title": "Renamed Track"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Renamed Track"
        assert data["id"] == 1

    async def test_update_track_partial(self, app_client):
        resp = await app_client.put(
            "/api/v1/library/tracks/1",
            json={"track_number": 99},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["track_number"] == 99
        # Other fields unchanged
        assert data["title"] is not None

    async def test_update_track_404(self, app_client):
        resp = await app_client.put(
            "/api/v1/library/tracks/9999",
            json={"title": "Nope"},
        )
        assert resp.status_code == 404

    async def test_delete_track(self, app_client):
        resp = await app_client.delete("/api/v1/library/tracks/3")
        assert resp.status_code == 204

        resp = await app_client.get("/api/v1/library/tracks/3")
        assert resp.status_code == 404

    async def test_delete_track_404(self, app_client):
        resp = await app_client.delete("/api/v1/library/tracks/9999")
        assert resp.status_code == 404


class TestAlbumEditing:
    async def test_update_album(self, app_client):
        resp = await app_client.put(
            "/api/v1/library/albums/1",
            json={"title": "New Album Title", "year": 2001},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "New Album Title"
        assert data["year"] == 2001

    async def test_update_album_partial(self, app_client):
        resp = await app_client.put(
            "/api/v1/library/albums/1",
            json={"genre": "Rock"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["genre"] == "Rock"
        assert data["title"] == "Fantaisie Militaire"

    async def test_update_album_404(self, app_client):
        resp = await app_client.put(
            "/api/v1/library/albums/9999",
            json={"title": "Nope"},
        )
        assert resp.status_code == 404

    async def test_delete_album(self, app_client):
        resp = await app_client.delete("/api/v1/library/albums/1")
        assert resp.status_code == 204

        resp = await app_client.get("/api/v1/library/albums/1")
        assert resp.status_code == 404

    async def test_delete_album_404(self, app_client):
        resp = await app_client.delete("/api/v1/library/albums/9999")
        assert resp.status_code == 404


class TestArtistEditing:
    async def test_update_artist(self, app_client):
        resp = await app_client.put(
            "/api/v1/library/artists/1",
            json={"sort_name": "Bashung, Alain", "bio": "French singer"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["sort_name"] == "Bashung, Alain"
        assert data["bio"] == "French singer"

    async def test_update_artist_partial(self, app_client):
        resp = await app_client.put(
            "/api/v1/library/artists/1",
            json={"name": "A. Bashung"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "A. Bashung"

    async def test_update_artist_404(self, app_client):
        resp = await app_client.put(
            "/api/v1/library/artists/9999",
            json={"name": "Nope"},
        )
        assert resp.status_code == 404

    async def test_delete_artist(self, app_client):
        resp = await app_client.delete("/api/v1/library/artists/1")
        assert resp.status_code == 204

        resp = await app_client.get("/api/v1/library/artists/1")
        assert resp.status_code == 404

    async def test_delete_artist_404(self, app_client):
        resp = await app_client.delete("/api/v1/library/artists/9999")
        assert resp.status_code == 404


class TestArtwork:
    async def test_get_artwork(self, app_client, tmp_path, monkeypatch):
        # Create a fake artwork file
        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        art_file = art_dir / "cover123.jpg"
        art_file.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-data")

        monkeypatch.setattr("tune_server.api.routes.library.settings.artwork_cache_dir", str(art_dir))

        resp = await app_client.get("/api/v1/library/artwork/cover123.jpg")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"

    async def test_get_artwork_png(self, app_client, tmp_path, monkeypatch):
        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        art_file = art_dir / "cover.png"
        art_file.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-data")

        monkeypatch.setattr("tune_server.api.routes.library.settings.artwork_cache_dir", str(art_dir))

        resp = await app_client.get("/api/v1/library/artwork/cover.png")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"

    async def test_get_artwork_404(self, app_client, tmp_path, monkeypatch):
        art_dir = tmp_path / "artwork"
        art_dir.mkdir()

        monkeypatch.setattr("tune_server.api.routes.library.settings.artwork_cache_dir", str(art_dir))

        resp = await app_client.get("/api/v1/library/artwork/nonexistent.jpg")
        assert resp.status_code == 404

    async def test_get_artwork_path_traversal_dotdot(self, app_client, tmp_path, monkeypatch):
        """Encoded slashes are rejected at routing or handler level."""
        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        monkeypatch.setattr("tune_server.api.routes.library.settings.artwork_cache_dir", str(art_dir))

        # URL-encoded slashes — ASGI/FastAPI splits path segments, so this 404s
        resp = await app_client.get("/api/v1/library/artwork/..%2F..%2Fetc%2Fpasswd")
        assert resp.status_code in (400, 404)

        # Literal ".." without slashes — hits the handler, caught by validation
        resp = await app_client.get("/api/v1/library/artwork/..cover.jpg")
        assert resp.status_code == 400

    async def test_get_artwork_path_traversal_slash(self, app_client, tmp_path, monkeypatch):
        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        monkeypatch.setattr("tune_server.api.routes.library.settings.artwork_cache_dir", str(art_dir))

        resp = await app_client.get("/api/v1/library/artwork/foo%2Fbar.jpg")
        assert resp.status_code in (400, 404)

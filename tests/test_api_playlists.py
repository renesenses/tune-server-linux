from __future__ import annotations

import pytest


class TestPlaylistCRUD:
    async def test_create_playlist(self, app_client):
        resp = await app_client.post(
            "/api/v1/playlists",
            json={"name": "My Playlist", "description": "Test playlist"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My Playlist"
        assert data["description"] == "Test playlist"
        assert data["id"] is not None
        assert data["track_count"] == 0

    async def test_create_playlist_minimal(self, app_client):
        resp = await app_client.post(
            "/api/v1/playlists",
            json={"name": "Bare Playlist"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Bare Playlist"
        assert data["description"] is None

    async def test_list_playlists_empty(self, app_client):
        resp = await app_client.get("/api/v1/playlists")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_playlists(self, app_client):
        await app_client.post("/api/v1/playlists", json={"name": "Playlist A"})
        await app_client.post("/api/v1/playlists", json={"name": "Playlist B"})
        resp = await app_client.get("/api/v1/playlists")
        assert resp.status_code == 200
        playlists = resp.json()
        assert len(playlists) == 2

    async def test_list_playlists_pagination(self, app_client):
        await app_client.post("/api/v1/playlists", json={"name": "A"})
        await app_client.post("/api/v1/playlists", json={"name": "B"})
        await app_client.post("/api/v1/playlists", json={"name": "C"})
        resp = await app_client.get("/api/v1/playlists", params={"limit": 2, "offset": 1})
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_get_playlist(self, app_client):
        create = await app_client.post(
            "/api/v1/playlists",
            json={"name": "Fetch Me"},
        )
        pid = create.json()["id"]
        resp = await app_client.get(f"/api/v1/playlists/{pid}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Fetch Me"

    async def test_get_playlist_404(self, app_client):
        resp = await app_client.get("/api/v1/playlists/9999")
        assert resp.status_code == 404

    async def test_update_playlist(self, app_client):
        create = await app_client.post(
            "/api/v1/playlists",
            json={"name": "Old Name"},
        )
        pid = create.json()["id"]
        resp = await app_client.put(
            f"/api/v1/playlists/{pid}",
            json={"name": "New Name", "description": "Updated"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "New Name"
        assert data["description"] == "Updated"

    async def test_update_playlist_partial(self, app_client):
        create = await app_client.post(
            "/api/v1/playlists",
            json={"name": "Original", "description": "Keep me"},
        )
        pid = create.json()["id"]
        resp = await app_client.put(
            f"/api/v1/playlists/{pid}",
            json={"name": "Changed"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Changed"
        # description unchanged
        assert data["description"] == "Keep me"

    async def test_update_playlist_404(self, app_client):
        resp = await app_client.put(
            "/api/v1/playlists/9999",
            json={"name": "Nope"},
        )
        assert resp.status_code == 404

    async def test_delete_playlist(self, app_client):
        create = await app_client.post(
            "/api/v1/playlists",
            json={"name": "Delete Me"},
        )
        pid = create.json()["id"]
        resp = await app_client.delete(f"/api/v1/playlists/{pid}")
        assert resp.status_code == 204

        # Verify gone
        resp = await app_client.get(f"/api/v1/playlists/{pid}")
        assert resp.status_code == 404

    async def test_delete_playlist_404(self, app_client):
        resp = await app_client.delete("/api/v1/playlists/9999")
        assert resp.status_code == 404


class TestPlaylistTracks:
    async def _create_playlist(self, client):
        resp = await client.post(
            "/api/v1/playlists",
            json={"name": "Track Test"},
        )
        return resp.json()["id"]

    async def test_get_tracks_empty(self, app_client):
        pid = await self._create_playlist(app_client)
        resp = await app_client.get(f"/api/v1/playlists/{pid}/tracks")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_add_tracks(self, app_client):
        pid = await self._create_playlist(app_client)
        resp = await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [1, 2, 3]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["track_count"] == 3

    async def test_add_tracks_returns_updated_playlist(self, app_client):
        pid = await self._create_playlist(app_client)
        resp = await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [1]},
        )
        assert resp.status_code == 200
        assert resp.json()["track_count"] == 1

        resp = await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [2, 3]},
        )
        assert resp.json()["track_count"] == 3

    async def test_get_tracks_ordered(self, app_client):
        pid = await self._create_playlist(app_client)
        await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [3, 1, 2]},
        )
        resp = await app_client.get(f"/api/v1/playlists/{pid}/tracks")
        assert resp.status_code == 200
        tracks = resp.json()
        assert len(tracks) == 3
        assert tracks[0]["id"] == 3
        assert tracks[1]["id"] == 1
        assert tracks[2]["id"] == 2

    async def test_add_tracks_at_position(self, app_client):
        pid = await self._create_playlist(app_client)
        await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [1, 3]},
        )
        # Insert track 2 at position 1 (between 1 and 3)
        await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [2], "position": 1},
        )
        resp = await app_client.get(f"/api/v1/playlists/{pid}/tracks")
        tracks = resp.json()
        assert [t["id"] for t in tracks] == [1, 2, 3]

    async def test_remove_track(self, app_client):
        pid = await self._create_playlist(app_client)
        await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [1, 2, 3]},
        )
        resp = await app_client.delete(f"/api/v1/playlists/{pid}/tracks/2")
        assert resp.status_code == 204

        resp = await app_client.get(f"/api/v1/playlists/{pid}/tracks")
        tracks = resp.json()
        assert len(tracks) == 2
        assert [t["id"] for t in tracks] == [1, 3]

    async def test_remove_track_closes_gap(self, app_client):
        pid = await self._create_playlist(app_client)
        await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [1, 2, 3]},
        )
        # Remove the first track
        await app_client.delete(f"/api/v1/playlists/{pid}/tracks/1")
        # Add a new track — should append correctly
        await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [1]},
        )
        resp = await app_client.get(f"/api/v1/playlists/{pid}/tracks")
        tracks = resp.json()
        assert len(tracks) == 3
        assert [t["id"] for t in tracks] == [2, 3, 1]

    async def test_reorder_tracks(self, app_client):
        pid = await self._create_playlist(app_client)
        await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [1, 2, 3]},
        )
        resp = await app_client.put(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [3, 1, 2]},
        )
        assert resp.status_code == 200
        tracks = resp.json()
        assert [t["id"] for t in tracks] == [3, 1, 2]

    async def test_add_tracks_playlist_404(self, app_client):
        resp = await app_client.post(
            "/api/v1/playlists/9999/tracks",
            json={"track_ids": [1]},
        )
        assert resp.status_code == 404

    async def test_remove_track_playlist_404(self, app_client):
        resp = await app_client.delete("/api/v1/playlists/9999/tracks/1")
        assert resp.status_code == 404

    async def test_reorder_tracks_playlist_404(self, app_client):
        resp = await app_client.put(
            "/api/v1/playlists/9999/tracks",
            json={"track_ids": [1]},
        )
        assert resp.status_code == 404

    async def test_get_tracks_playlist_404(self, app_client):
        resp = await app_client.get("/api/v1/playlists/9999/tracks")
        assert resp.status_code == 404

    async def test_delete_playlist_cascades_tracks(self, app_client):
        pid = await self._create_playlist(app_client)
        await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [1, 2]},
        )
        await app_client.delete(f"/api/v1/playlists/{pid}")
        # Playlist gone
        resp = await app_client.get(f"/api/v1/playlists/{pid}")
        assert resp.status_code == 404

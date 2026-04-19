"""Tests for the playlist manager routes — merge, snapshots, dedup, reorder, streaming tracks."""

from __future__ import annotations

import json

import pytest


class TestPlaylistMerge:
    """Tests for POST /api/v1/playlist-manager/merge."""

    async def _create_playlist_with_tracks(self, client, name, track_ids):
        """Helper: create a playlist and add tracks to it."""
        resp = await client.post("/api/v1/playlists", json={"name": name})
        assert resp.status_code == 201
        pid = resp.json()["id"]
        if track_ids:
            await client.post(
                f"/api/v1/playlists/{pid}/tracks",
                json={"track_ids": track_ids},
            )
        return pid

    async def test_merge_two_local_playlists(self, app_client, sample_tracks):
        """Merging two local playlists should create a new playlist with all tracks."""
        pid_a = await self._create_playlist_with_tracks(
            app_client, "Playlist A", [sample_tracks[0].id, sample_tracks[1].id]
        )
        pid_b = await self._create_playlist_with_tracks(
            app_client, "Playlist B", [sample_tracks[2].id]
        )

        resp = await app_client.post(
            "/api/v1/playlist-manager/merge",
            json={
                "playlists": [
                    {"service": "local", "playlist_id": str(pid_a)},
                    {"service": "local", "playlist_id": str(pid_b)},
                ],
                "target_name": "Merged Playlist",
                "deduplicate": True,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["playlist_name"] == "Merged Playlist"
        assert data["total_tracks"] == 3
        assert data["added_to_playlist"] == 3
        assert data["sources"] == 2

    async def test_merge_with_dedup(self, app_client, sample_tracks):
        """Merging playlists with shared tracks should deduplicate when enabled."""
        t0, t1, t2 = sample_tracks[0].id, sample_tracks[1].id, sample_tracks[2].id
        pid_a = await self._create_playlist_with_tracks(
            app_client, "Overlap A", [t0, t1]
        )
        pid_b = await self._create_playlist_with_tracks(
            app_client, "Overlap B", [t1, t2]
        )

        resp = await app_client.post(
            "/api/v1/playlist-manager/merge",
            json={
                "playlists": [
                    {"service": "local", "playlist_id": str(pid_a)},
                    {"service": "local", "playlist_id": str(pid_b)},
                ],
                "target_name": "Deduped Merge",
                "deduplicate": True,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # Track t1 appears in both, but dedup removes the second occurrence.
        # The merge logic deduplicates by title|artist_name, so the actual
        # dedup result depends on the title being the same for the same track.
        assert data["total_tracks"] == 3  # 3 unique tracks
        assert data["sources"] == 2

    async def test_merge_without_dedup(self, app_client, sample_tracks):
        """Merging playlists without dedup should keep all tracks including duplicates."""
        t0, t1 = sample_tracks[0].id, sample_tracks[1].id
        pid_a = await self._create_playlist_with_tracks(
            app_client, "No Dedup A", [t0, t1]
        )
        pid_b = await self._create_playlist_with_tracks(
            app_client, "No Dedup B", [t0, t1]
        )

        resp = await app_client.post(
            "/api/v1/playlist-manager/merge",
            json={
                "playlists": [
                    {"service": "local", "playlist_id": str(pid_a)},
                    {"service": "local", "playlist_id": str(pid_b)},
                ],
                "target_name": "All Tracks",
                "deduplicate": False,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # Without dedup, all 4 entries are kept
        assert data["total_tracks"] == 4
        assert data["sources"] == 2

    async def test_merge_empty_playlists(self, app_client):
        """Merging empty playlists should produce an empty merged playlist."""
        pid_a = await self._create_playlist_with_tracks(app_client, "Empty A", [])
        pid_b = await self._create_playlist_with_tracks(app_client, "Empty B", [])

        resp = await app_client.post(
            "/api/v1/playlist-manager/merge",
            json={
                "playlists": [
                    {"service": "local", "playlist_id": str(pid_a)},
                    {"service": "local", "playlist_id": str(pid_b)},
                ],
                "target_name": "Empty Merge",
                "deduplicate": True,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_tracks"] == 0
        assert data["added_to_playlist"] == 0


class TestPlaylistSnapshots:
    """Tests for playlist snapshot (backup) CRUD routes."""

    async def _create_snapshot(self, db, service, pid, name, tracks):
        """Helper: insert a snapshot row directly via db."""
        await db.execute(
            """INSERT INTO playlist_snapshots
               (source_service, source_playlist_id, playlist_name, track_count, snapshot_data)
               VALUES (?, ?, ?, ?, ?)""",
            (service, str(pid), name, len(tracks), json.dumps(tracks)),
        )
        await db.commit()
        row = await db.fetchone(
            "SELECT id FROM playlist_snapshots ORDER BY id DESC LIMIT 1"
        )
        return row["id"]

    async def test_list_snapshots_empty(self, app_client):
        """Listing snapshots with no backups returns an empty list."""
        resp = await app_client.get("/api/v1/playlist-manager/backups")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_and_list_snapshot(self, app_client, db, sample_tracks):
        """After creating a snapshot directly, it should appear in the list."""
        tracks_data = [
            {"title": t.title, "artist_name": t.artist_name} for t in sample_tracks
        ]
        snap_id = await self._create_snapshot(
            db, "local", "1", "Test Snapshot", tracks_data
        )

        resp = await app_client.get("/api/v1/playlist-manager/backups")
        assert resp.status_code == 200
        snapshots = resp.json()
        assert len(snapshots) >= 1
        assert any(s["id"] == snap_id for s in snapshots)

    async def test_get_snapshot_detail(self, app_client, db, sample_tracks):
        """Fetching a single snapshot should include track data."""
        tracks_data = [
            {"title": t.title, "artist_name": t.artist_name} for t in sample_tracks
        ]
        snap_id = await self._create_snapshot(
            db, "local", "1", "Detail Snapshot", tracks_data
        )

        resp = await app_client.get(f"/api/v1/playlist-manager/backups/{snap_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["playlist_name"] == "Detail Snapshot"
        assert data["track_count"] == 3
        assert len(data["tracks"]) == 3
        assert data["tracks"][0]["title"] == sample_tracks[0].title

    async def test_get_snapshot_404(self, app_client):
        """Fetching a non-existent snapshot should return 404."""
        resp = await app_client.get("/api/v1/playlist-manager/backups/9999")
        assert resp.status_code == 404

    async def test_delete_snapshot(self, app_client, db, sample_tracks):
        """Deleting a snapshot should remove it from the database."""
        snap_id = await self._create_snapshot(
            db, "local", "1", "Delete Me", [{"title": "t"}]
        )

        resp = await app_client.delete(f"/api/v1/playlist-manager/backups/{snap_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Verify gone
        resp = await app_client.get(f"/api/v1/playlist-manager/backups/{snap_id}")
        assert resp.status_code == 404

    async def test_delete_snapshot_404(self, app_client):
        """Deleting a non-existent snapshot should return 404."""
        resp = await app_client.delete("/api/v1/playlist-manager/backups/9999")
        assert resp.status_code == 404

    async def test_restore_snapshot(self, app_client, db, sample_tracks):
        """Restoring a snapshot should create a local playlist with matched tracks."""
        tracks_data = [
            {"title": t.title, "artist_name": t.artist_name} for t in sample_tracks
        ]
        snap_id = await self._create_snapshot(
            db, "local", "1", "Restore Me", tracks_data
        )

        resp = await app_client.post(
            f"/api/v1/playlist-manager/backups/{snap_id}/restore",
            json={"target_name": "Restored Playlist"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Restored Playlist"
        assert data["tracks_restored"] >= 1
        assert data["local_playlist_id"] is not None

    async def test_restore_snapshot_default_name(self, app_client, db, sample_tracks):
        """Restoring without a target_name should use the snapshot's original name."""
        tracks_data = [
            {"title": t.title, "artist_name": t.artist_name} for t in sample_tracks
        ]
        snap_id = await self._create_snapshot(
            db, "local", "1", "Original Name", tracks_data
        )

        resp = await app_client.post(
            f"/api/v1/playlist-manager/backups/{snap_id}/restore",
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Original Name"

    async def test_restore_snapshot_conflict_without_overwrite(
        self, app_client, db, sample_tracks
    ):
        """Restoring a snapshot when a playlist with the same name exists should 409."""
        # Create a local playlist with the same name
        await app_client.post("/api/v1/playlists", json={"name": "Conflict Name"})

        tracks_data = [{"title": sample_tracks[0].title, "artist_name": sample_tracks[0].artist_name}]
        snap_id = await self._create_snapshot(
            db, "local", "1", "Conflict Name", tracks_data
        )

        resp = await app_client.post(
            f"/api/v1/playlist-manager/backups/{snap_id}/restore",
        )
        assert resp.status_code == 409

    async def test_restore_snapshot_with_overwrite(
        self, app_client, db, sample_tracks
    ):
        """Restoring a snapshot with overwrite_existing=true should succeed."""
        await app_client.post("/api/v1/playlists", json={"name": "Overwrite Me"})

        tracks_data = [
            {"title": t.title, "artist_name": t.artist_name} for t in sample_tracks
        ]
        snap_id = await self._create_snapshot(
            db, "local", "1", "Overwrite Me", tracks_data
        )

        resp = await app_client.post(
            f"/api/v1/playlist-manager/backups/{snap_id}/restore",
            json={"target_name": "Overwrite Me", "overwrite_existing": True},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Overwrite Me"

    async def test_restore_snapshot_404(self, app_client):
        """Restoring a non-existent snapshot should return 404."""
        resp = await app_client.post(
            "/api/v1/playlist-manager/backups/9999/restore",
        )
        assert resp.status_code == 404

    async def test_list_snapshots_filter_by_service(self, app_client, db):
        """Filtering snapshots by source_service should only return matching ones."""
        await self._create_snapshot(db, "local", "1", "Local Snap", [])
        await self._create_snapshot(db, "tidal", "abc", "Tidal Snap", [])

        resp = await app_client.get(
            "/api/v1/playlist-manager/backups", params={"service": "local"}
        )
        assert resp.status_code == 200
        snapshots = resp.json()
        assert all(s["source_service"] == "local" for s in snapshots)
        assert len(snapshots) == 1


class TestAddDuplicateTracks:
    """Tests for dedup behavior when adding tracks to a playlist."""

    async def _create_playlist(self, client, name="Dedup Test"):
        resp = await client.post("/api/v1/playlists", json={"name": name})
        return resp.json()["id"]

    async def test_add_same_track_twice(self, app_client, sample_tracks):
        """Adding a track that already exists should not duplicate it."""
        pid = await self._create_playlist(app_client)
        t0 = sample_tracks[0].id

        # Add track the first time
        resp = await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [t0]},
        )
        assert resp.status_code == 200
        assert resp.json()["track_count"] == 1

        # Add the same track again
        resp = await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [t0]},
        )
        assert resp.status_code == 200
        # track_count should still be 1, not 2
        assert resp.json()["track_count"] == 1

    async def test_add_batch_with_duplicates(self, app_client, sample_tracks):
        """Adding a batch containing duplicates should deduplicate within the batch."""
        pid = await self._create_playlist(app_client)
        t0, t1 = sample_tracks[0].id, sample_tracks[1].id

        resp = await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [t0, t1, t0, t1, t0]},
        )
        assert resp.status_code == 200
        assert resp.json()["track_count"] == 2

    async def test_add_mix_existing_and_new(self, app_client, sample_tracks):
        """Adding a mix of existing and new tracks should only insert the new ones."""
        pid = await self._create_playlist(app_client)
        t0, t1, t2 = [t.id for t in sample_tracks]

        # Add t0
        await app_client.post(
            f"/api/v1/playlists/{pid}/tracks", json={"track_ids": [t0]}
        )

        # Add t0 (existing) + t1, t2 (new)
        resp = await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [t0, t1, t2]},
        )
        assert resp.status_code == 200
        assert resp.json()["track_count"] == 3

        # Verify order: t0 first, then t1, t2 appended
        resp = await app_client.get(f"/api/v1/playlists/{pid}/tracks")
        tracks = resp.json()
        assert [t["id"] for t in tracks] == [t0, t1, t2]

    async def test_add_all_duplicates_returns_unchanged(self, app_client, sample_tracks):
        """Adding only duplicates should not change the track count."""
        pid = await self._create_playlist(app_client)
        t0, t1 = sample_tracks[0].id, sample_tracks[1].id

        await app_client.post(
            f"/api/v1/playlists/{pid}/tracks", json={"track_ids": [t0, t1]}
        )

        resp = await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [t1, t0]},
        )
        assert resp.status_code == 200
        assert resp.json()["track_count"] == 2


class TestPlaylistReorderExtended:
    """Extended reorder tests beyond the basic case covered in test_api_playlists."""

    async def _create_playlist_with_tracks(self, client, track_ids):
        resp = await client.post("/api/v1/playlists", json={"name": "Reorder Test"})
        pid = resp.json()["id"]
        await client.post(
            f"/api/v1/playlists/{pid}/tracks", json={"track_ids": track_ids}
        )
        return pid

    async def test_reverse_order(self, app_client, sample_tracks):
        """Reversing the track order should persist correctly."""
        ids = [t.id for t in sample_tracks]
        pid = await self._create_playlist_with_tracks(app_client, ids)

        reversed_ids = list(reversed(ids))
        resp = await app_client.put(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": reversed_ids},
        )
        assert resp.status_code == 200
        assert [t["id"] for t in resp.json()] == reversed_ids

    async def test_reorder_single_track(self, app_client, sample_tracks):
        """Reordering a playlist with a single track should work."""
        t0 = sample_tracks[0].id
        pid = await self._create_playlist_with_tracks(app_client, [t0])

        resp = await app_client.put(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [t0]},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["id"] == t0

    async def test_reorder_preserves_track_data(self, app_client, sample_tracks):
        """After reorder, each track's metadata should still be correct."""
        ids = [t.id for t in sample_tracks]
        pid = await self._create_playlist_with_tracks(app_client, ids)

        new_order = [ids[2], ids[0], ids[1]]
        resp = await app_client.put(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": new_order},
        )
        assert resp.status_code == 200
        tracks = resp.json()
        assert tracks[0]["title"] == sample_tracks[2].title
        assert tracks[1]["title"] == sample_tracks[0].title
        assert tracks[2]["title"] == sample_tracks[1].title

    async def test_reorder_to_empty_clears_playlist(self, app_client, sample_tracks):
        """Reordering with an empty list should clear all tracks."""
        ids = [t.id for t in sample_tracks]
        pid = await self._create_playlist_with_tracks(app_client, ids)

        resp = await app_client.put(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": []},
        )
        assert resp.status_code == 200
        assert resp.json() == []

        # Verify via GET
        resp = await app_client.get(f"/api/v1/playlists/{pid}/tracks")
        assert resp.json() == []

        # Track count should be 0
        resp = await app_client.get(f"/api/v1/playlists/{pid}")
        assert resp.json()["track_count"] == 0


class TestPlaylistWithStreamingTracks:
    """Tests for adding streaming service tracks to playlists."""

    async def _create_playlist(self, client, name="Streaming Test"):
        resp = await client.post("/api/v1/playlists", json={"name": name})
        return resp.json()["id"]

    async def test_add_streaming_track(self, app_client):
        """Adding a streaming track should upsert it into the tracks table and add to the playlist."""
        pid = await self._create_playlist(app_client)

        resp = await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={
                "track_ids": [],
                "streaming_tracks": [
                    {
                        "source": "tidal",
                        "source_id": "tidal-12345",
                        "title": "Bohemian Rhapsody",
                        "artist_name": "Queen",
                        "album_title": "A Night at the Opera",
                        "duration_ms": 354000,
                    }
                ],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["track_count"] == 1

        # Verify the track is in the playlist
        resp = await app_client.get(f"/api/v1/playlists/{pid}/tracks")
        tracks = resp.json()
        assert len(tracks) == 1
        assert tracks[0]["title"] == "Bohemian Rhapsody"
        assert tracks[0]["source"] == "tidal"
        assert tracks[0]["source_id"] == "tidal-12345"

    async def test_add_streaming_track_idempotent(self, app_client):
        """Adding the same streaming track to two playlists should reuse the track entry."""
        pid_a = await self._create_playlist(app_client, "Stream A")
        pid_b = await self._create_playlist(app_client, "Stream B")

        streaming_track = {
            "source": "qobuz",
            "source_id": "qobuz-999",
            "title": "Stairway to Heaven",
            "artist_name": "Led Zeppelin",
            "duration_ms": 482000,
        }

        await app_client.post(
            f"/api/v1/playlists/{pid_a}/tracks",
            json={"streaming_tracks": [streaming_track]},
        )
        await app_client.post(
            f"/api/v1/playlists/{pid_b}/tracks",
            json={"streaming_tracks": [streaming_track]},
        )

        # Both playlists should have 1 track
        resp_a = await app_client.get(f"/api/v1/playlists/{pid_a}/tracks")
        resp_b = await app_client.get(f"/api/v1/playlists/{pid_b}/tracks")
        tracks_a = resp_a.json()
        tracks_b = resp_b.json()
        assert len(tracks_a) == 1
        assert len(tracks_b) == 1
        # They should reference the same track ID
        assert tracks_a[0]["id"] == tracks_b[0]["id"]

    async def test_mix_local_and_streaming_tracks(self, app_client, sample_tracks):
        """Adding both local track IDs and streaming tracks in one request should work."""
        pid = await self._create_playlist(app_client, "Mixed")

        resp = await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={
                "track_ids": [sample_tracks[0].id],
                "streaming_tracks": [
                    {
                        "source": "tidal",
                        "source_id": "tidal-mix-1",
                        "title": "Hotel California",
                        "artist_name": "Eagles",
                        "duration_ms": 391000,
                    }
                ],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["track_count"] == 2

        # Check order: local first, then streaming
        resp = await app_client.get(f"/api/v1/playlists/{pid}/tracks")
        tracks = resp.json()
        assert len(tracks) == 2
        assert tracks[0]["id"] == sample_tracks[0].id
        assert tracks[1]["source"] == "tidal"

    async def test_add_streaming_track_at_position(self, app_client, sample_tracks):
        """Adding a streaming track at a specific position should insert correctly."""
        pid = await self._create_playlist(app_client, "Position Test")

        # First add two local tracks
        await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={"track_ids": [sample_tracks[0].id, sample_tracks[2].id]},
        )

        # Insert streaming track at position 1 (between the two)
        resp = await app_client.post(
            f"/api/v1/playlists/{pid}/tracks",
            json={
                "streaming_tracks": [
                    {
                        "source": "tidal",
                        "source_id": "tidal-insert-1",
                        "title": "Comfortably Numb",
                        "artist_name": "Pink Floyd",
                        "duration_ms": 382000,
                    }
                ],
                "position": 1,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["track_count"] == 3

        resp = await app_client.get(f"/api/v1/playlists/{pid}/tracks")
        tracks = resp.json()
        assert tracks[0]["id"] == sample_tracks[0].id
        assert tracks[1]["source"] == "tidal"
        assert tracks[2]["id"] == sample_tracks[2].id


class TestPlaylistSyncLinks:
    """Tests for sync link CRUD (the link management, not the actual sync with a service)."""

    async def _create_playlist(self, client, name="Sync Link Test"):
        resp = await client.post("/api/v1/playlists", json={"name": name})
        return resp.json()["id"]

    async def test_create_link(self, app_client):
        """Creating a sync link should return the link with all fields."""
        pid = await self._create_playlist(app_client)

        resp = await app_client.post(
            "/api/v1/playlist-manager/links",
            json={
                "local_playlist_id": pid,
                "service": "tidal",
                "service_playlist_id": "tidal-pl-001",
                "sync_direction": "pull",
                "sync_interval_minutes": 60,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["local_playlist_id"] == pid
        assert data["service"] == "tidal"
        assert data["service_playlist_id"] == "tidal-pl-001"
        assert data["sync_direction"] == "pull"
        assert data["sync_interval_minutes"] == 60
        assert data["id"] is not None

    async def test_list_links(self, app_client):
        """Listing links after creating some should return them all."""
        pid = await self._create_playlist(app_client)

        await app_client.post(
            "/api/v1/playlist-manager/links",
            json={
                "local_playlist_id": pid,
                "service": "tidal",
                "service_playlist_id": "tidal-link-1",
                "sync_direction": "pull",
            },
        )
        await app_client.post(
            "/api/v1/playlist-manager/links",
            json={
                "local_playlist_id": pid,
                "service": "qobuz",
                "service_playlist_id": "qobuz-link-1",
                "sync_direction": "push",
            },
        )

        resp = await app_client.get("/api/v1/playlist-manager/links")
        assert resp.status_code == 200
        links = resp.json()
        assert len(links) == 2

    async def test_update_link(self, app_client):
        """Updating a link's direction and interval should persist."""
        pid = await self._create_playlist(app_client)

        create_resp = await app_client.post(
            "/api/v1/playlist-manager/links",
            json={
                "local_playlist_id": pid,
                "service": "tidal",
                "service_playlist_id": "tidal-upd-1",
                "sync_direction": "pull",
                "sync_interval_minutes": 0,
            },
        )
        link_id = create_resp.json()["id"]

        resp = await app_client.patch(
            f"/api/v1/playlist-manager/links/{link_id}",
            json={
                "sync_direction": "bidirectional",
                "sync_interval_minutes": 120,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["sync_direction"] == "bidirectional"
        assert data["sync_interval_minutes"] == 120

    async def test_update_link_404(self, app_client):
        """Updating a non-existent link should return 404."""
        resp = await app_client.patch(
            "/api/v1/playlist-manager/links/9999",
            json={"sync_direction": "push"},
        )
        assert resp.status_code == 404

    async def test_delete_link(self, app_client):
        """Deleting a link should remove it from the list."""
        pid = await self._create_playlist(app_client)

        create_resp = await app_client.post(
            "/api/v1/playlist-manager/links",
            json={
                "local_playlist_id": pid,
                "service": "tidal",
                "service_playlist_id": "tidal-del-1",
                "sync_direction": "pull",
            },
        )
        link_id = create_resp.json()["id"]

        resp = await app_client.delete(f"/api/v1/playlist-manager/links/{link_id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify gone
        resp = await app_client.get("/api/v1/playlist-manager/links")
        assert all(l["id"] != link_id for l in resp.json())

    async def test_update_link_invalid_direction(self, app_client):
        """Updating a link with an invalid sync_direction should return 400."""
        pid = await self._create_playlist(app_client)

        create_resp = await app_client.post(
            "/api/v1/playlist-manager/links",
            json={
                "local_playlist_id": pid,
                "service": "tidal",
                "service_playlist_id": "tidal-inv-1",
                "sync_direction": "pull",
            },
        )
        link_id = create_resp.json()["id"]

        resp = await app_client.patch(
            f"/api/v1/playlist-manager/links/{link_id}",
            json={"sync_direction": "invalid_direction"},
        )
        assert resp.status_code == 400


class TestTransferHistory:
    """Tests for the transfer history endpoint."""

    async def test_history_empty(self, app_client):
        """Transfer history should be empty initially."""
        resp = await app_client.get("/api/v1/playlist-manager/history")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_history_returns_entries(self, app_client, db):
        """After inserting a history entry, it should appear in the list."""
        await db.execute(
            """INSERT INTO transfer_history
               (operation, source_service, source_playlist_name, target_service,
                target_playlist_name, total_tracks, matched, approximate, not_found, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("transfer", "tidal", "My Tidal PL", "local", "My Local PL", 10, 8, 1, 1, "completed"),
        )
        await db.commit()

        resp = await app_client.get("/api/v1/playlist-manager/history")
        assert resp.status_code == 200
        history = resp.json()
        assert len(history) == 1
        entry = history[0]
        assert entry["operation"] == "transfer"
        assert entry["total_tracks"] == 10
        assert entry["matched"] == 8

    async def test_history_detail_404(self, app_client):
        """Fetching detail for a non-existent transfer should return 404."""
        resp = await app_client.get("/api/v1/playlist-manager/history/9999")
        assert resp.status_code == 404

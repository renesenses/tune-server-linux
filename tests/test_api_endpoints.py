"""Comprehensive API endpoint tests.

Tests all critical API endpoints using httpx AsyncClient against the
in-process FastAPI app. Some endpoints depend on optional subsystems
(scanner, discovery, streaming services) — those are mocked or skipped
as appropriate.

Run with:  pytest tests/test_api_endpoints.py -v
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest


# =========================================================================
# 1. System endpoints
# =========================================================================

class TestSystemHealth:
    async def test_health_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/system/health")
        assert resp.status_code == 200

    async def test_health_has_status(self, app_client):
        data = (await app_client.get("/api/v1/system/health")).json()
        assert data["status"] in ("ok", "degraded")

    async def test_health_has_components(self, app_client):
        data = (await app_client.get("/api/v1/system/health")).json()
        assert "components" in data
        assert isinstance(data["components"], dict)


class TestSystemConfig:
    async def test_config_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/system/config")
        assert resp.status_code == 200

    async def test_config_has_music_dirs(self, app_client):
        data = (await app_client.get("/api/v1/system/config")).json()
        assert "music_dirs" in data

    async def test_config_has_ports(self, app_client):
        data = (await app_client.get("/api/v1/system/config")).json()
        assert "api_port" in data
        assert "stream_port" in data

    async def test_config_has_sync_settings(self, app_client):
        data = (await app_client.get("/api/v1/system/config")).json()
        assert "sync_poll_playing_interval" in data
        assert "sync_poll_idle_interval" in data
        assert "sync_drift_threshold_ms" in data

    async def test_config_has_db_info(self, app_client):
        data = (await app_client.get("/api/v1/system/config")).json()
        assert "db_engine" in data
        assert "db_connected" in data


class TestSystemDiagnostics:
    async def test_diagnostics_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/system/diagnostics")
        assert resp.status_code == 200

    async def test_diagnostics_has_version(self, app_client):
        data = (await app_client.get("/api/v1/system/diagnostics")).json()
        assert "version" in data
        assert isinstance(data["version"], str)

    async def test_diagnostics_has_platform_info(self, app_client):
        data = (await app_client.get("/api/v1/system/diagnostics")).json()
        assert "python" in data
        assert "platform" in data
        assert "pid" in data

    async def test_diagnostics_has_counts(self, app_client):
        data = (await app_client.get("/api/v1/system/diagnostics")).json()
        assert "tracks_count" in data
        assert "albums_count" in data
        assert "artists_count" in data

    async def test_diagnostics_has_db_section(self, app_client):
        data = (await app_client.get("/api/v1/system/diagnostics")).json()
        assert "db" in data
        assert "engine" in data["db"]


class TestSystemHealthMonitor:
    async def test_health_monitor_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/system/health/monitor")
        assert resp.status_code == 200

    async def test_health_monitor_has_checks(self, app_client):
        data = (await app_client.get("/api/v1/system/health/monitor")).json()
        assert "checks" in data
        assert "status" in data

    async def test_health_alerts_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/system/health/alerts")
        assert resp.status_code == 200


class TestSystemPlugins:
    async def test_plugins_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/system/plugins")
        assert resp.status_code == 200

    async def test_plugins_returns_list(self, app_client):
        data = (await app_client.get("/api/v1/system/plugins")).json()
        assert isinstance(data, list)


class TestSystemStats:
    async def test_stats_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/system/stats")
        assert resp.status_code == 200

    async def test_stats_has_counts(self, app_client):
        data = (await app_client.get("/api/v1/system/stats")).json()
        assert "tracks" in data
        assert "albums" in data
        assert "artists" in data
        assert "zones" in data
        assert "devices" in data

    async def test_stats_counts_match_sample_data(self, app_client):
        data = (await app_client.get("/api/v1/system/stats")).json()
        assert data["tracks"] == 3
        assert data["albums"] == 1
        assert data["artists"] == 1


class TestSystemScan:
    async def test_scan_status_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/system/scan/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "scanning" in data

    async def test_scan_trigger_no_scanner(self, app_client):
        from tune_server.api.deps import deps
        deps.scanner = None
        resp = await app_client.post("/api/v1/system/scan")
        assert resp.status_code == 503

    async def test_scan_trigger_with_scanner(self, app_client):
        from tune_server.api.deps import deps
        mock_scanner = MagicMock()
        mock_scanner.is_scanning = False
        mock_scanner.scan = AsyncMock()
        deps.scanner = mock_scanner
        try:
            resp = await app_client.post("/api/v1/system/scan")
            assert resp.status_code == 202
            data = resp.json()
            assert data["status"] == "scan_started"
        finally:
            deps.scanner = None

    async def test_scan_trigger_already_scanning(self, app_client):
        from tune_server.api.deps import deps
        mock_scanner = MagicMock()
        mock_scanner.is_scanning = True
        deps.scanner = mock_scanner
        try:
            resp = await app_client.post("/api/v1/system/scan")
            assert resp.status_code == 409
        finally:
            deps.scanner = None


class TestSystemDatabaseStatus:
    async def test_database_status_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/system/database/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "engine" in data
        assert "connected" in data

    async def test_database_test_returns_200(self, app_client):
        resp = await app_client.post("/api/v1/system/database/test")
        assert resp.status_code == 200
        data = resp.json()
        assert "ok" in data


class TestSystemMode:
    async def test_get_mode(self, app_client):
        resp = await app_client.get("/api/v1/system/mode")
        assert resp.status_code == 200
        data = resp.json()
        assert "mode" in data


# =========================================================================
# 2. Zone endpoints
# =========================================================================

class TestZones:
    async def test_list_zones_empty(self, app_client_with_zones):
        resp = await app_client_with_zones.get("/api/v1/zones")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_zone_returns_201(self, app_client_with_zones):
        resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Salon", "output_type": "local"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Salon"
        assert data["output_type"] == "local"
        assert data["id"] is not None

    async def test_get_zone_returns_200(self, app_client_with_zones):
        create_resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Cuisine", "output_type": "local"},
        )
        zone_id = create_resp.json()["id"]
        resp = await app_client_with_zones.get(f"/api/v1/zones/{zone_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Cuisine"

    async def test_get_zone_404(self, app_client_with_zones):
        resp = await app_client_with_zones.get("/api/v1/zones/99999")
        assert resp.status_code == 404

    async def test_delete_zone_returns_204(self, app_client_with_zones):
        create_resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Temporaire", "output_type": "local"},
        )
        zone_id = create_resp.json()["id"]
        resp = await app_client_with_zones.delete(f"/api/v1/zones/{zone_id}")
        assert resp.status_code == 204

    async def test_delete_zone_verify_gone(self, app_client_with_zones):
        create_resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Gone Zone", "output_type": "local"},
        )
        zone_id = create_resp.json()["id"]
        await app_client_with_zones.delete(f"/api/v1/zones/{zone_id}")
        resp = await app_client_with_zones.get(f"/api/v1/zones/{zone_id}")
        assert resp.status_code == 404

    async def test_delete_zone_404(self, app_client_with_zones):
        resp = await app_client_with_zones.delete("/api/v1/zones/99999")
        assert resp.status_code == 404

    async def test_update_zone(self, app_client_with_zones):
        create_resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Original", "output_type": "local"},
        )
        zone_id = create_resp.json()["id"]
        resp = await app_client_with_zones.put(
            f"/api/v1/zones/{zone_id}",
            json={"name": "Updated", "sync_delay_ms": 100},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"
        assert resp.json()["sync_delay_ms"] == 100

    async def test_patch_zone(self, app_client_with_zones):
        create_resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Patch Me", "output_type": "local"},
        )
        zone_id = create_resp.json()["id"]
        resp = await app_client_with_zones.patch(
            f"/api/v1/zones/{zone_id}",
            json={"sync_delay_ms": -50},
        )
        assert resp.status_code == 200
        assert resp.json()["sync_delay_ms"] == -50

    async def test_list_zones_after_creation(self, app_client_with_zones):
        await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Zone A", "output_type": "local"},
        )
        await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Zone B", "output_type": "local"},
        )
        resp = await app_client_with_zones.get("/api/v1/zones")
        assert resp.status_code == 200
        zones = resp.json()
        assert len(zones) == 2
        names = {z["name"] for z in zones}
        assert "Zone A" in names
        assert "Zone B" in names


class TestZoneGroups:
    async def test_list_groups_empty(self, app_client_with_zones):
        resp = await app_client_with_zones.get("/api/v1/zones/groups/list")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_group_zones(self, app_client_with_zones):
        r1 = await app_client_with_zones.post(
            "/api/v1/zones", json={"name": "Leader", "output_type": "local"},
        )
        r2 = await app_client_with_zones.post(
            "/api/v1/zones", json={"name": "Follower", "output_type": "local"},
        )
        lid = r1.json()["id"]
        fid = r2.json()["id"]
        resp = await app_client_with_zones.post(
            "/api/v1/zones/group",
            json={"leader_id": lid, "zone_ids": [lid, fid]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "group_id" in data
        assert data["leader_id"] == lid

    async def test_group_leader_not_found(self, app_client_with_zones):
        resp = await app_client_with_zones.post(
            "/api/v1/zones/group",
            json={"leader_id": 999, "zone_ids": [999, 998]},
        )
        assert resp.status_code == 404


# =========================================================================
# 3. Library endpoints
# =========================================================================

class TestLibraryTracks:
    async def test_list_tracks_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/library/tracks")
        assert resp.status_code == 200

    async def test_list_tracks_returns_sample_data(self, app_client):
        data = (await app_client.get("/api/v1/library/tracks")).json()
        assert isinstance(data, list)
        assert len(data) == 3

    async def test_list_tracks_with_limit(self, app_client):
        resp = await app_client.get("/api/v1/library/tracks", params={"limit": 1})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1

    async def test_list_tracks_with_offset(self, app_client):
        resp = await app_client.get("/api/v1/library/tracks", params={"limit": 10, "offset": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1  # 3 total, skip 2 = 1 left

    async def test_list_tracks_pagination(self, app_client):
        resp = await app_client.get("/api/v1/library/tracks", params={"page": 1, "per_page": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert "tracks" in data
        assert "total" in data
        assert "page" in data
        assert data["total"] == 3
        assert data["page"] == 1
        assert len(data["tracks"]) == 2

    async def test_tracks_count_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/library/tracks/count")
        assert resp.status_code == 200

    async def test_tracks_count_has_count_field(self, app_client):
        data = (await app_client.get("/api/v1/library/tracks/count")).json()
        assert "count" in data
        assert data["count"] == 3

    async def test_get_track_by_id(self, app_client):
        resp = await app_client.get("/api/v1/library/tracks/1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 1
        assert "title" in data

    async def test_get_track_not_found(self, app_client):
        resp = await app_client.get("/api/v1/library/tracks/99999")
        assert resp.status_code == 404


class TestLibraryAlbums:
    async def test_list_albums_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/library/albums")
        assert resp.status_code == 200

    async def test_list_albums_returns_sample_data(self, app_client):
        data = (await app_client.get("/api/v1/library/albums")).json()
        assert isinstance(data, list)
        assert len(data) == 1

    async def test_get_album_by_id(self, app_client):
        resp = await app_client.get("/api/v1/library/albums/1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Fantaisie Militaire"
        assert data["artist_name"] == "Alain Bashung"

    async def test_get_album_tracks(self, app_client):
        resp = await app_client.get("/api/v1/library/albums/1/tracks")
        assert resp.status_code == 200
        tracks = resp.json()
        assert len(tracks) == 3

    async def test_get_album_not_found(self, app_client):
        resp = await app_client.get("/api/v1/library/albums/99999")
        assert resp.status_code == 404

    async def test_albums_count(self, app_client):
        resp = await app_client.get("/api/v1/library/albums/count")
        assert resp.status_code == 200
        data = resp.json()
        assert "count" in data
        assert data["count"] == 1


class TestLibraryArtists:
    async def test_list_artists_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/library/artists")
        assert resp.status_code == 200

    async def test_list_artists_returns_sample_data(self, app_client):
        data = (await app_client.get("/api/v1/library/artists")).json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "Alain Bashung"

    async def test_get_artist_by_id(self, app_client):
        resp = await app_client.get("/api/v1/library/artists/1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Alain Bashung"

    async def test_get_artist_albums(self, app_client):
        resp = await app_client.get("/api/v1/library/artists/1/albums")
        assert resp.status_code == 200
        albums = resp.json()
        assert len(albums) == 1
        assert albums[0]["title"] == "Fantaisie Militaire"

    async def test_get_artist_tracks(self, app_client):
        resp = await app_client.get("/api/v1/library/artists/1/tracks")
        assert resp.status_code == 200
        tracks = resp.json()
        assert len(tracks) == 3

    async def test_get_artist_not_found(self, app_client):
        resp = await app_client.get("/api/v1/library/artists/99999")
        assert resp.status_code == 404


class TestLibrarySearch:
    async def test_search_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/library/search", params={"q": "Bashung"})
        assert resp.status_code == 200

    async def test_search_returns_results(self, app_client):
        data = (await app_client.get("/api/v1/library/search", params={"q": "Bashung"})).json()
        assert "tracks" in data or "albums" in data or "artists" in data

    async def test_search_missing_query(self, app_client):
        resp = await app_client.get("/api/v1/library/search")
        assert resp.status_code == 422  # missing required param

    async def test_search_no_results(self, app_client):
        resp = await app_client.get("/api/v1/library/search", params={"q": "zzzznonexistent"})
        assert resp.status_code == 200


class TestLibraryStats:
    async def test_library_stats_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/library/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "tracks" in data
        assert "albums" in data
        assert "artists" in data


class TestFederatedSearch:
    async def test_federated_search_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/search", params={"q": "test"})
        assert resp.status_code == 200

    async def test_federated_search_missing_query(self, app_client):
        resp = await app_client.get("/api/v1/search")
        assert resp.status_code == 422


# =========================================================================
# 4. Playback endpoints (require zones)
# =========================================================================

class TestPlayback:
    async def test_play_zone_not_found(self, app_client_with_zones):
        resp = await app_client_with_zones.post("/api/v1/zones/99999/play")
        assert resp.status_code == 404

    async def test_pause_zone_not_found(self, app_client_with_zones):
        resp = await app_client_with_zones.post("/api/v1/zones/99999/pause")
        assert resp.status_code == 404

    async def test_stop_zone_not_found(self, app_client_with_zones):
        resp = await app_client_with_zones.post("/api/v1/zones/99999/stop")
        assert resp.status_code == 404

    async def test_play_on_zone(self, app_client_with_zones):
        create_resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Play Zone", "output_type": "local"},
        )
        zone_id = create_resp.json()["id"]
        # Play with no tracks (resume) -- should return the zone model
        resp = await app_client_with_zones.post(f"/api/v1/zones/{zone_id}/play")
        # May succeed (200) or fail with 502 depending on player state
        assert resp.status_code in (200, 502)

    async def test_pause_on_zone(self, app_client_with_zones):
        create_resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Pause Zone", "output_type": "local"},
        )
        zone_id = create_resp.json()["id"]
        resp = await app_client_with_zones.post(f"/api/v1/zones/{zone_id}/pause")
        assert resp.status_code in (200, 502)

    async def test_stop_on_zone(self, app_client_with_zones):
        create_resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Stop Zone", "output_type": "local"},
        )
        zone_id = create_resp.json()["id"]
        resp = await app_client_with_zones.post(f"/api/v1/zones/{zone_id}/stop")
        assert resp.status_code in (200, 502)


class TestEqualizer:
    async def test_eq_zone_not_found(self, app_client_with_zones):
        resp = await app_client_with_zones.get("/api/v1/zones/99999/eq")
        assert resp.status_code == 404

    async def test_get_eq_on_zone(self, app_client_with_zones):
        create_resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "EQ Zone", "output_type": "local"},
        )
        zone_id = create_resp.json()["id"]
        resp = await app_client_with_zones.get(f"/api/v1/zones/{zone_id}/eq")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data


class TestAudiophile:
    async def test_audiophile_zone_not_found(self, app_client_with_zones):
        resp = await app_client_with_zones.get("/api/v1/zones/99999/audiophile")
        assert resp.status_code == 404

    async def test_get_audiophile_on_zone(self, app_client_with_zones):
        create_resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Audiophile Zone", "output_type": "local"},
        )
        zone_id = create_resp.json()["id"]
        resp = await app_client_with_zones.get(f"/api/v1/zones/{zone_id}/audiophile")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data
        assert "effects_disabled" in data


class TestCrossfade:
    async def test_get_crossfade_on_zone(self, app_client_with_zones):
        create_resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Crossfade Zone", "output_type": "local"},
        )
        zone_id = create_resp.json()["id"]
        resp = await app_client_with_zones.get(f"/api/v1/zones/{zone_id}/crossfade")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data
        assert "duration" in data


class TestQueue:
    async def test_queue_zone_not_found(self, app_client_with_zones):
        resp = await app_client_with_zones.get("/api/v1/zones/99999/queue")
        assert resp.status_code == 404

    async def test_get_queue_on_zone(self, app_client_with_zones):
        create_resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Queue Zone", "output_type": "local"},
        )
        zone_id = create_resp.json()["id"]
        resp = await app_client_with_zones.get(f"/api/v1/zones/{zone_id}/queue")
        assert resp.status_code == 200

    async def test_queue_has_length_field(self, app_client_with_zones):
        create_resp = await app_client_with_zones.post(
            "/api/v1/zones",
            json={"name": "Queue Len Zone", "output_type": "local"},
        )
        zone_id = create_resp.json()["id"]
        resp = await app_client_with_zones.get(f"/api/v1/zones/{zone_id}/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert "length" in data
        assert data["length"] == 0


# =========================================================================
# 5. Streaming endpoints
# =========================================================================

class TestStreamingServices:
    async def test_services_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/streaming/services")
        assert resp.status_code == 200

    async def test_services_returns_dict(self, app_client):
        data = (await app_client.get("/api/v1/streaming/services")).json()
        assert isinstance(data, dict)

    async def test_services_has_known_services(self, app_client):
        data = (await app_client.get("/api/v1/streaming/services")).json()
        # All known streaming services should be listed
        for svc in ("tidal", "qobuz", "youtube", "spotify", "deezer", "amazon"):
            assert svc in data
            assert "enabled" in data[svc]
            assert "authenticated" in data[svc]

    async def test_service_status_disabled(self, app_client):
        resp = await app_client.get("/api/v1/streaming/tidal/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["authenticated"] is False

    async def test_service_search_unavailable(self, app_client):
        resp = await app_client.get("/api/v1/streaming/tidal/search", params={"q": "test"})
        assert resp.status_code == 503

    async def test_qobuz_status_disabled(self, app_client):
        resp = await app_client.get("/api/v1/streaming/qobuz/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False


class TestStreamingYouTube:
    async def test_youtube_status(self, app_client):
        resp = await app_client.get("/api/v1/streaming/youtube/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data


# =========================================================================
# 6. Device endpoints
# =========================================================================

class TestDevices:
    async def test_list_devices_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/devices")
        assert resp.status_code == 200

    async def test_list_devices_no_discovery(self, app_client):
        from tune_server.api.deps import deps
        deps.discovery_manager = None
        resp = await app_client.get("/api/v1/devices")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_scan_devices_no_discovery(self, app_client):
        from tune_server.api.deps import deps
        deps.discovery_manager = None
        resp = await app_client.post("/api/v1/devices/scan")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_scan_devices_with_discovery(self, app_client):
        from tune_server.api.deps import deps
        mock_dm = MagicMock()
        mock_dm.rescan = AsyncMock(return_value=[])
        deps.discovery_manager = mock_dm
        try:
            resp = await app_client.post("/api/v1/devices/scan")
            assert resp.status_code == 200
            assert resp.json() == []
            mock_dm.rescan.assert_awaited_once()
        finally:
            deps.discovery_manager = None


# =========================================================================
# 7. Root endpoint
# =========================================================================

class TestRoot:
    async def test_root_returns_200(self, app_client):
        resp = await app_client.get("/")
        assert resp.status_code == 200

    async def test_root_content(self, app_client):
        resp = await app_client.get("/")
        ct = resp.headers.get("content-type", "")
        if ct.startswith("application/json"):
            data = resp.json()
            assert data["name"] == "Tune Server"
            assert "api" in data
            assert "docs" in data
        else:
            # SPA mode: web/ bundle served
            assert "html" in ct.lower()


# =========================================================================
# 8. Additional system endpoints
# =========================================================================

class TestListeningStats:
    async def test_listening_stats_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/system/stats/listening")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_plays" in data
        assert "top_artists" in data
        assert "top_albums" in data
        assert "recent" in data
        assert "plays_by_day" in data


class TestBackups:
    async def test_list_backups_returns_200(self, app_client):
        resp = await app_client.get("/api/v1/system/backups")
        assert resp.status_code == 200


class TestConfigPatch:
    async def test_patch_config_no_fields(self, app_client):
        resp = await app_client.patch("/api/v1/system/config", json={})
        assert resp.status_code == 400

    async def test_patch_config_metadata_readonly(self, app_client):
        resp = await app_client.patch(
            "/api/v1/system/config",
            json={"metadata_readonly": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "metadata_readonly" in data

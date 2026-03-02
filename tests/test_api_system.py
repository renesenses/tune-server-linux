from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# --- System ---


async def test_config(app_client):
    resp = await app_client.get("/api/v1/system/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "music_dirs" in data
    assert "api_port" in data
    assert "stream_port" in data
    assert "sync_poll_playing_interval" in data
    assert "sync_poll_idle_interval" in data
    assert "sync_drift_threshold_ms" in data
    assert "sync_correction_cooldown_s" in data
    assert "sync_dlna_default_buffer_s" in data


async def test_scan_trigger(app_client):
    from tune_server.api.deps import deps

    mock_scanner = MagicMock()
    mock_scanner.is_scanning = False
    mock_scanner.scan = AsyncMock()
    deps.scanner = mock_scanner

    resp = await app_client.post("/api/v1/system/scan")
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "scan_started"

    deps.scanner = None


async def test_scan_status(app_client):
    from tune_server.api.deps import deps

    mock_scanner = MagicMock()
    mock_scanner.is_scanning = True
    deps.scanner = mock_scanner

    resp = await app_client.get("/api/v1/system/scan/status")
    assert resp.status_code == 200
    assert resp.json()["scanning"] is True

    deps.scanner = None


async def test_system_stats(app_client):
    resp = await app_client.get("/api/v1/system/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "tracks" in data
    assert "albums" in data
    assert "artists" in data


# --- Devices ---


async def test_list_devices_no_discovery(app_client):
    from tune_server.api.deps import deps

    deps.discovery_manager = None
    resp = await app_client.get("/api/v1/devices")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_device_no_discovery(app_client):
    from fastapi.exceptions import ResponseValidationError
    from tune_server.api.deps import deps

    deps.discovery_manager = None
    # The route returns {"error": ...} which doesn't match DiscoveredDevice model
    with pytest.raises(ResponseValidationError):
        await app_client.get("/api/v1/devices/some-id")


# --- Streaming ---


async def test_tidal_status_disabled(app_client):
    from tune_server.api.deps import deps

    deps.tidal = None
    resp = await app_client.get("/api/v1/streaming/tidal/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert data["authenticated"] is False


async def test_tidal_search_unavailable(app_client):
    from tune_server.api.deps import deps

    deps.tidal = None
    resp = await app_client.get("/api/v1/streaming/tidal/search?q=test")
    assert resp.status_code == 503


async def test_qobuz_status_disabled(app_client):
    from tune_server.api.deps import deps

    deps.qobuz = None
    resp = await app_client.get("/api/v1/streaming/qobuz/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert data["authenticated"] is False


async def test_qobuz_search_unavailable(app_client):
    from tune_server.api.deps import deps

    deps.qobuz = None
    resp = await app_client.get("/api/v1/streaming/qobuz/search?q=test")
    assert resp.status_code == 503


async def test_tidal_auth_not_configured(app_client):
    from tune_server.api.deps import deps

    deps.tidal = None
    resp = await app_client.post("/api/v1/streaming/tidal/auth")
    assert resp.status_code == 503


async def test_qobuz_auth_not_configured(app_client):
    from tune_server.api.deps import deps

    deps.qobuz = None
    resp = await app_client.post("/api/v1/streaming/qobuz/auth", json={"username": "test", "password": "test"})
    assert resp.status_code == 503

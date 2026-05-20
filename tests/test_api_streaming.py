from __future__ import annotations

import builtins
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import httpx
import pytest

from tune_server.api.deps import deps
from tune_server.api.main import create_api_app
from tune_server.streaming.base import StreamingService

_real_import = builtins.__import__


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_service(
    name: str = "tidal",
    authenticated: bool = False,
    iframe_only: bool = False,
) -> AsyncMock:
    """Create a mock StreamingService with sensible defaults."""
    svc = AsyncMock(spec=StreamingService)
    svc.name = name
    type(svc).is_authenticated = PropertyMock(return_value=authenticated)
    svc.iframe_only = iframe_only
    svc.authenticate = AsyncMock(return_value=True)
    svc.save_auth = AsyncMock()
    svc.restore_auth = AsyncMock(return_value=False)
    svc.disconnect = AsyncMock()
    svc.verification_url = None
    svc.user_code = None
    svc._auth_error = None
    return svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def streaming_client(db, event_bus):
    """Minimal ASGI test client with no streaming services wired up."""
    from tune_server.db.sa_repository import (
        AlbumRepo, ArtistRepo, PlaylistRepo, PlayQueueRepo, TrackRepo, ZoneRepo,
    )

    deps.db = db
    deps.event_bus = event_bus
    deps.track_repo = TrackRepo(db)
    deps.album_repo = AlbumRepo(db)
    deps.artist_repo = ArtistRepo(db)
    deps.playlist_repo = PlaylistRepo(db)
    deps.queue_repo = PlayQueueRepo(db)
    deps.zone_repo = ZoneRepo(db)
    deps.streaming_services = {}

    app = create_api_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client

    deps.db = None
    deps.event_bus = None
    deps.track_repo = None
    deps.album_repo = None
    deps.artist_repo = None
    deps.playlist_repo = None
    deps.queue_repo = None
    deps.zone_repo = None
    deps.streaming_services = {}


# ===================================================================
# GET /streaming/services
# ===================================================================

class TestListServices:
    async def test_lists_all_six_services(self, streaming_client):
        resp = await streaming_client.get("/api/v1/streaming/services")
        assert resp.status_code == 200
        data = resp.json()
        expected = {"tidal", "qobuz", "youtube", "spotify", "deezer", "amazon"}
        assert set(data.keys()) == expected

    async def test_all_disabled_by_default(self, streaming_client):
        resp = await streaming_client.get("/api/v1/streaming/services")
        data = resp.json()
        for name, status in data.items():
            assert status["enabled"] is False, f"{name} should be disabled"
            assert status["authenticated"] is False

    async def test_enabled_service_shown(self, streaming_client):
        deps.streaming_services["tidal"] = _make_mock_service("tidal", authenticated=True)
        resp = await streaming_client.get("/api/v1/streaming/services")
        data = resp.json()
        assert data["tidal"]["enabled"] is True
        assert data["tidal"]["authenticated"] is True
        # Others still disabled
        assert data["qobuz"]["enabled"] is False

    async def test_iframe_only_flag(self, streaming_client):
        svc = _make_mock_service("youtube", authenticated=True, iframe_only=True)
        deps.streaming_services["youtube"] = svc
        resp = await streaming_client.get("/api/v1/streaming/services")
        data = resp.json()
        assert data["youtube"]["iframe_only"] is True


# ===================================================================
# POST /streaming/{service}/enable
# ===================================================================

class TestEnableService:
    async def test_enable_unknown_service_404(self, streaming_client):
        resp = await streaming_client.post("/api/v1/streaming/napster/enable")
        assert resp.status_code == 404
        assert "Unknown service" in resp.json()["detail"]

    async def test_enable_already_enabled(self, streaming_client):
        deps.streaming_services["tidal"] = _make_mock_service("tidal")
        resp = await streaming_client.post("/api/v1/streaming/tidal/enable")
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_enabled"

    @patch("tune_server.config.persist_env_var")
    async def test_enable_tidal_persists_and_creates(self, mock_persist, streaming_client):
        """Enable tidal: persists TUNE_TIDAL_ENABLED=true + quality, creates instance."""
        fake_svc = _make_mock_service("tidal")
        fake_module = MagicMock()
        fake_module.TidalService = MagicMock(return_value=fake_svc)

        def side_effect(name, *args, **kwargs):
            if name == "tune_server.streaming.tidal":
                return fake_module
            return _real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=side_effect):
            resp = await streaming_client.post("/api/v1/streaming/tidal/enable")

        assert resp.status_code == 200
        assert resp.json()["status"] == "enabled"

        # Verify persist calls
        persist_calls = {call.args[0]: call.args[1] for call in mock_persist.call_args_list}
        assert persist_calls["TUNE_TIDAL_ENABLED"] == "true"
        assert "TUNE_TIDAL_QUALITY" in persist_calls

        # Service instance was registered
        assert "tidal" in deps.streaming_services

    @patch("tune_server.config.persist_env_var")
    async def test_enable_qobuz_persists_app_id_and_secret(self, mock_persist, streaming_client):
        """Enable qobuz: persists app_id and app_secret."""
        fake_svc = _make_mock_service("qobuz")
        fake_module = MagicMock()
        fake_module.QobuzService = MagicMock(return_value=fake_svc)

        def side_effect(name, *args, **kwargs):
            if name == "tune_server.streaming.qobuz":
                return fake_module
            return _real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=side_effect):
            resp = await streaming_client.post("/api/v1/streaming/qobuz/enable")

        assert resp.status_code == 200
        assert resp.json()["status"] == "enabled"

        persist_calls = {call.args[0]: call.args[1] for call in mock_persist.call_args_list}
        assert persist_calls["TUNE_QOBUZ_ENABLED"] == "true"
        assert "TUNE_QOBUZ_APP_ID" in persist_calls
        assert "TUNE_QOBUZ_APP_SECRET" in persist_calls

    @patch("tune_server.config.persist_env_var")
    async def test_enable_deezer_persists_quality(self, mock_persist, streaming_client):
        fake_svc = _make_mock_service("deezer")
        fake_module = MagicMock()
        fake_module.DeezerService = MagicMock(return_value=fake_svc)

        def side_effect(name, *args, **kwargs):
            if name == "tune_server.streaming.deezer":
                return fake_module
            return _real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=side_effect):
            resp = await streaming_client.post("/api/v1/streaming/deezer/enable")

        assert resp.status_code == 200
        persist_calls = {call.args[0]: call.args[1] for call in mock_persist.call_args_list}
        assert persist_calls["TUNE_DEEZER_ENABLED"] == "true"
        assert "TUNE_DEEZER_QUALITY" in persist_calls

    @patch("tune_server.config.persist_env_var")
    async def test_enable_amazon_persists_region_and_quality(self, mock_persist, streaming_client):
        fake_svc = _make_mock_service("amazon")
        fake_module = MagicMock()
        fake_module.AmazonMusicService = MagicMock(return_value=fake_svc)

        def side_effect(name, *args, **kwargs):
            if name == "tune_server.streaming.amazon":
                return fake_module
            return _real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=side_effect):
            resp = await streaming_client.post("/api/v1/streaming/amazon/enable")

        assert resp.status_code == 200
        persist_calls = {call.args[0]: call.args[1] for call in mock_persist.call_args_list}
        assert persist_calls["TUNE_AMAZON_ENABLED"] == "true"
        assert "TUNE_AMAZON_MUSIC_REGION" in persist_calls
        assert "TUNE_AMAZON_MUSIC_QUALITY" in persist_calls


# ===================================================================
# POST /streaming/{service}/disable
# ===================================================================

class TestDisableService:
    @patch("tune_server.config.persist_env_var")
    async def test_disable_running_service(self, mock_persist, streaming_client):
        svc = _make_mock_service("tidal")
        deps.streaming_services["tidal"] = svc

        resp = await streaming_client.post("/api/v1/streaming/tidal/disable")
        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"

        # Service removed from deps
        assert "tidal" not in deps.streaming_services
        # Persisted TUNE_TIDAL_ENABLED=false
        mock_persist.assert_any_call("TUNE_TIDAL_ENABLED", "false")
        # disconnect called
        svc.disconnect.assert_awaited_once()

    @patch("tune_server.config.persist_env_var")
    async def test_disable_already_disabled(self, mock_persist, streaming_client):
        """Disabling a service that is not running is safe (no error)."""
        resp = await streaming_client.post("/api/v1/streaming/qobuz/disable")
        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"
        mock_persist.assert_any_call("TUNE_QOBUZ_ENABLED", "false")

    async def test_disable_unknown_service_404(self, streaming_client):
        resp = await streaming_client.post("/api/v1/streaming/napster/disable")
        assert resp.status_code == 404


# ===================================================================
# POST /streaming/{service}/auth
# ===================================================================

class TestAuth:
    async def test_auth_service_not_configured_503(self, streaming_client):
        resp = await streaming_client.post("/api/v1/streaming/tidal/auth")
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]

    async def test_auth_success(self, streaming_client):
        svc = _make_mock_service("tidal", authenticated=False)
        svc.authenticate = AsyncMock(return_value=True)
        deps.streaming_services["tidal"] = svc

        resp = await streaming_client.post(
            "/api/v1/streaming/tidal/auth",
            json={"username": "user@example.com", "password": "s3cret"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        svc.authenticate.assert_awaited_once()
        svc.save_auth.assert_awaited_once()

    async def test_auth_failure(self, streaming_client):
        svc = _make_mock_service("tidal", authenticated=False)
        svc.authenticate = AsyncMock(return_value=False)
        svc._auth_error = "Invalid credentials"
        deps.streaming_services["tidal"] = svc

        resp = await streaming_client.post(
            "/api/v1/streaming/tidal/auth",
            json={"username": "user@example.com", "password": "wrong"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False
        assert data["error"] == "Invalid credentials"
        # save_auth NOT called on failure
        svc.save_auth.assert_not_awaited()

    async def test_auth_with_oauth_json(self, streaming_client):
        svc = _make_mock_service("spotify")
        deps.streaming_services["spotify"] = svc

        resp = await streaming_client.post(
            "/api/v1/streaming/spotify/auth",
            json={"oauth_json": '{"token": "abc123"}'},
        )
        assert resp.status_code == 200
        svc.authenticate.assert_awaited_once()
        # Verify oauth_json was passed through
        call_kwargs = svc.authenticate.call_args.kwargs
        assert call_kwargs["oauth_json"] == '{"token": "abc123"}'

    async def test_auth_no_body(self, streaming_client):
        """Auth with no request body (e.g. device-flow OAuth like Tidal)."""
        svc = _make_mock_service("tidal")
        svc.verification_url = "https://link.tidal.com/ABCDE"
        svc.user_code = "ABCDE"
        deps.streaming_services["tidal"] = svc

        resp = await streaming_client.post("/api/v1/streaming/tidal/auth")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["verification_url"] == "https://link.tidal.com/ABCDE"
        assert data["user_code"] == "ABCDE"


# ===================================================================
# POST /streaming/{service}/disconnect
# ===================================================================

class TestDisconnect:
    async def test_disconnect_not_configured_503(self, streaming_client):
        resp = await streaming_client.post("/api/v1/streaming/tidal/disconnect")
        assert resp.status_code == 503

    async def test_disconnect_success(self, streaming_client):
        svc = _make_mock_service("tidal", authenticated=True)
        deps.streaming_services["tidal"] = svc

        resp = await streaming_client.post("/api/v1/streaming/tidal/disconnect")
        assert resp.status_code == 200
        assert resp.json()["disconnected"] is True
        svc.disconnect.assert_awaited_once()


# ===================================================================
# GET /streaming/{service}/status
# ===================================================================

class TestServiceStatus:
    async def test_status_disabled(self, streaming_client):
        resp = await streaming_client.get("/api/v1/streaming/tidal/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["authenticated"] is False

    async def test_status_enabled_authenticated(self, streaming_client):
        deps.streaming_services["tidal"] = _make_mock_service("tidal", authenticated=True)
        resp = await streaming_client.get("/api/v1/streaming/tidal/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["authenticated"] is True

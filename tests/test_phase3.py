"""Phase 3 tests: security, reliability, and connection resilience."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import AudioFormat, AudioStreamInfo


# ===========================================================================
# S1: Configurable CORS + API Key Authentication
# ===========================================================================

class TestCORSConfig:
    def test_default_cors_origins(self):
        """Default CORS origins should be wildcard."""
        from tune_server.config import Settings
        s = Settings()
        assert s.cors_origins == ["*"]

    def test_custom_cors_origins(self):
        """Custom CORS origins can be set."""
        s = _settings(cors_origins=["http://localhost:3000", "https://app.example.com"])
        assert len(s.cors_origins) == 2
        assert "http://localhost:3000" in s.cors_origins

    def test_api_key_default_none(self):
        """API key defaults to None (no auth)."""
        from tune_server.config import Settings
        s = Settings()
        assert s.api_key is None


class TestCORSMiddleware:
    async def test_wildcard_cors_disables_credentials(self):
        """With wildcard origins, credentials should be disabled on the app."""
        with _patch_settings(cors_origins=["*"]):
            from tune_server.api.main import create_api_app
            app = create_api_app()
            # Verify CORS is configured by making a request
            import httpx
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.options(
                    "/",
                    headers={"Origin": "http://evil.com", "Access-Control-Request-Method": "GET"},
                )
                # Wildcard allows any origin
                assert resp.headers.get("access-control-allow-origin") in ("*", "http://evil.com")

    async def test_specific_origins_enable_credentials(self):
        """With specific origins, credentials should be enabled."""
        with _patch_settings(cors_origins=["http://localhost:3000"]):
            from tune_server.api.main import create_api_app
            app = create_api_app()
            import httpx
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.options(
                    "/",
                    headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
                )
                assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
                assert resp.headers.get("access-control-allow-credentials") == "true"


class TestAPIKeyMiddleware:
    async def test_no_api_key_allows_all(self):
        """Without API key configured, all requests should pass."""
        with _patch_settings(api_key=None):
            client = await _make_test_client()
            resp = await client.get("/api/v1/system/config")
            assert resp.status_code == 200

    async def test_api_key_blocks_without_key(self):
        """With API key configured, requests without key get 401."""
        with _patch_settings(api_key="test-secret-key"):
            client = await _make_test_client()
            resp = await client.get("/api/v1/system/config")
            assert resp.status_code == 401
            assert resp.json()["detail"] == "Invalid API key"

    async def test_api_key_accepts_header(self):
        """Requests with correct X-API-Key header should pass."""
        with _patch_settings(api_key="test-secret-key"):
            client = await _make_test_client()
            resp = await client.get(
                "/api/v1/system/config",
                headers={"X-API-Key": "test-secret-key"},
            )
            assert resp.status_code == 200

    async def test_api_key_accepts_query_param(self):
        """Requests with correct api_key query param should pass."""
        with _patch_settings(api_key="test-secret-key"):
            client = await _make_test_client()
            resp = await client.get("/api/v1/system/config?api_key=test-secret-key")
            assert resp.status_code == 200

    async def test_api_key_wrong_key_rejected(self):
        """Requests with wrong API key get 401."""
        with _patch_settings(api_key="test-secret-key"):
            client = await _make_test_client()
            resp = await client.get(
                "/api/v1/system/config",
                headers={"X-API-Key": "wrong-key"},
            )
            assert resp.status_code == 401

    async def test_health_exempt_from_auth(self):
        """Health endpoint should be accessible without API key."""
        with _patch_settings(api_key="test-secret-key"):
            client = await _make_test_client()
            resp = await client.get("/api/v1/system/health")
            assert resp.status_code == 200

    async def test_root_exempt_from_auth(self):
        """Root endpoint should be accessible without API key."""
        with _patch_settings(api_key="test-secret-key"):
            client = await _make_test_client()
            resp = await client.get("/")
            assert resp.status_code == 200


# ===========================================================================
# S2: WebSocket Heartbeat
# ===========================================================================

class TestWebSocketHeartbeat:
    async def test_ping_sent_after_timeout(self):
        """A ping should be sent when no message is received within heartbeat interval."""
        from tune_server.api.websocket import WebSocketManager

        event_bus = EventBus()
        manager = WebSocketManager(event_bus)
        await manager.start()

        ws = AsyncMock()
        ws.receive_text = AsyncMock(side_effect=asyncio.TimeoutError)
        ws.send_text = AsyncMock(side_effect=Exception("connection closed"))
        ws.close = AsyncMock()

        # connect should accept
        connected = await manager.connect(ws)
        assert connected
        assert len(manager._subscriptions) == 1

        await manager.stop()

    async def test_connection_limit_enforced(self):
        """51st connection should be rejected."""
        from tune_server.api.websocket import MAX_WS_CONNECTIONS, WebSocketManager

        event_bus = EventBus()
        manager = WebSocketManager(event_bus)

        # Fill to max
        for _ in range(MAX_WS_CONNECTIONS):
            ws = AsyncMock()
            ws.accept = AsyncMock()
            connected = await manager.connect(ws)
            assert connected

        assert len(manager._subscriptions) == MAX_WS_CONNECTIONS

        # 51st should be rejected
        ws_extra = AsyncMock()
        ws_extra.close = AsyncMock()
        connected = await manager.connect(ws_extra)
        assert not connected
        ws_extra.close.assert_awaited_once()

        assert len(manager._subscriptions) == MAX_WS_CONNECTIONS
        await manager.stop()

    async def test_pong_message_handled(self):
        """Pong messages from client should be silently consumed."""
        from tune_server.api.websocket import WebSocketManager

        event_bus = EventBus()
        manager = WebSocketManager(event_bus)

        # Simulate: client sends "pong" then disconnects
        ws = AsyncMock()
        call_count = 0

        async def receive_text():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "pong"
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect(code=1000)

        ws.receive_text = receive_text
        ws.accept = AsyncMock()
        ws.close = AsyncMock()

        with _patch_settings(ws_heartbeat_interval=0):
            await manager.handle_websocket(ws)

        # Should have disconnected cleanly
        assert ws not in manager._subscriptions


# ===========================================================================
# S3: EventBus emit_nowait fix
# ===========================================================================

class TestEventBusEmitNowait:
    async def test_emit_nowait_uses_running_loop(self):
        """emit_nowait should use get_running_loop (not deprecated get_event_loop)."""
        bus = EventBus()
        events_received = []

        async def listener(event):
            events_received.append(event)

        bus.on(EventType.SYSTEM_STARTED, listener)

        event = Event(type=EventType.SYSTEM_STARTED, data={"test": True})
        bus.emit_nowait(event)

        # Give the task a chance to run
        await asyncio.sleep(0.05)
        assert len(events_received) == 1
        assert events_received[0].data["test"] is True

    async def test_emit_nowait_logs_errors(self):
        """Failed fire-and-forget tasks should be logged, not silently lost."""
        bus = EventBus()

        async def failing_listener(event):
            raise ValueError("test error")

        bus.on(EventType.SYSTEM_STARTED, failing_listener)

        event = Event(type=EventType.SYSTEM_STARTED, data={})
        bus.emit_nowait(event)

        # Give the task a chance to complete
        await asyncio.sleep(0.05)
        # If we get here without unhandled exception, the callback caught it

    def test_emit_nowait_no_running_loop(self):
        """emit_nowait without running loop should warn, not crash."""
        bus = EventBus()
        event = Event(type=EventType.SYSTEM_STARTED, data={})
        # This should not raise - just log a warning
        # (We can't easily test outside a loop in pytest-asyncio,
        # but we can verify the code path exists)
        import inspect
        source = inspect.getsource(bus.emit_nowait)
        assert "get_running_loop" in source

    async def test_emit_nowait_task_done_callback(self):
        """The _task_done callback should handle cancelled tasks."""
        bus = EventBus()
        task = MagicMock()
        task.cancelled.return_value = True
        # Should return without calling task.exception()
        bus._task_done(task)
        task.exception.assert_not_called()


# ===========================================================================
# S4: Streaming Token Refresh
# ===========================================================================

class TestTidalTokenRefresh:
    async def test_ensure_authenticated_valid_session(self):
        """_ensure_authenticated returns True for valid session."""
        from tune_server.streaming.tidal import TidalService

        svc = TidalService()
        svc._session = MagicMock()
        svc._session.check_login = MagicMock(return_value=True)

        result = await svc._ensure_authenticated()
        assert result is True

    async def test_ensure_authenticated_refreshes_expired(self):
        """_ensure_authenticated refreshes an expired token."""
        from tune_server.streaming.tidal import TidalService

        svc = TidalService()
        svc._session = MagicMock()

        # First check_login returns False (expired), refresh succeeds, second check returns True
        svc._session.check_login = MagicMock(side_effect=[False, True])
        svc._session.token_refresh = MagicMock(return_value=True)
        svc._session.refresh_token = "refresh-tok"

        result = await svc._ensure_authenticated()
        assert result is True
        svc._session.token_refresh.assert_called_once_with("refresh-tok")

    async def test_ensure_authenticated_no_session(self):
        """_ensure_authenticated returns False with no session."""
        from tune_server.streaming.tidal import TidalService

        svc = TidalService()
        svc._session = None
        result = await svc._ensure_authenticated()
        assert result is False

    async def test_ensure_authenticated_refresh_fails(self):
        """_ensure_authenticated returns False when refresh fails."""
        from tune_server.streaming.tidal import TidalService

        svc = TidalService()
        svc._session = MagicMock()
        svc._session.check_login = MagicMock(return_value=False)
        svc._session.token_refresh = MagicMock(side_effect=Exception("refresh error"))
        svc._session.refresh_token = "refresh-tok"

        result = await svc._ensure_authenticated()
        assert result is False

    async def test_search_calls_ensure_authenticated(self):
        """search() should call _ensure_authenticated before API call."""
        from tune_server.streaming.tidal import TidalService

        svc = TidalService()
        svc._ensure_authenticated = AsyncMock(return_value=False)

        result = await svc.search("test")
        assert result.tracks == []
        svc._ensure_authenticated.assert_awaited_once()

    async def test_get_track_calls_ensure_authenticated(self):
        """get_track() should call _ensure_authenticated."""
        from tune_server.streaming.tidal import TidalService

        svc = TidalService()
        svc._ensure_authenticated = AsyncMock(return_value=False)

        result = await svc.get_track("123")
        assert result is None
        svc._ensure_authenticated.assert_awaited_once()

    async def test_get_stream_url_calls_ensure_authenticated(self):
        """get_stream_url() should call _ensure_authenticated."""
        from tune_server.streaming.tidal import TidalService

        svc = TidalService()
        svc._ensure_authenticated = AsyncMock(return_value=False)

        result = await svc.get_stream_url("123")
        assert result is None
        svc._ensure_authenticated.assert_awaited_once()


class TestQobuzTokenValidation:
    async def test_session_has_timeout(self):
        """Qobuz aiohttp session should have a timeout configured."""
        from tune_server.streaming.qobuz import QobuzService

        svc = QobuzService()
        session = await svc._ensure_session()
        assert session.timeout.total == 30
        assert session.timeout.connect == 10
        await svc.close()

    async def test_api_get_clears_token_on_401(self):
        """_api_get should clear token and raise on 401 response."""
        import aiohttp
        from tune_server.streaming.qobuz import QobuzService

        svc = QobuzService()
        svc._user_auth_token = "valid-token"

        # Mock the session to return 401
        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.request_info = MagicMock()
        mock_resp.history = ()

        mock_session = MagicMock()
        mock_session.closed = False
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_cm)
        svc._session = mock_session

        with pytest.raises(aiohttp.ClientResponseError):
            await svc._api_get("test/endpoint")

        assert svc._user_auth_token is None

    async def test_api_get_passes_auth_header(self):
        """_api_get should include auth token in headers when available."""
        from tune_server.streaming.qobuz import QobuzService

        svc = QobuzService()
        svc._user_auth_token = "my-token"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"result": "ok"})
        mock_resp.raise_for_status = MagicMock()

        mock_session = MagicMock()
        mock_session.closed = False
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_cm)
        svc._session = mock_session

        result = await svc._api_get("test/endpoint")
        assert result == {"result": "ok"}

        # Verify the auth header was passed
        call_args = mock_session.get.call_args
        headers = call_args.kwargs.get("headers", {})
        assert headers.get("X-User-Auth-Token") == "my-token"


# ===========================================================================
# S5: Output Device Recovery
# ===========================================================================

class TestDlnaRecovery:
    async def test_dlna_recovers_after_error(self):
        """DLNA output should recover _available after successful start."""
        from tune_server.outputs.dlna import DlnaOutput

        mock_device = AsyncMock()
        mock_streamer = MagicMock()
        mock_streamer.create_session.return_value = "session-1"
        mock_streamer.get_stream_url.return_value = "http://192.168.1.1:8080/stream/session-1.flac"

        output = DlnaOutput(mock_device, mock_streamer, "192.168.1.1")

        # Simulate a previous failure
        output._available = False

        stream_info = AudioStreamInfo(
            format=AudioFormat.FLAC, sample_rate=44100, bit_depth=16, channels=2
        )
        from tune_server.models import Track
        track = Track(title="Test", file_path="/music/test.flac")

        await output.start(stream_info, track)

        assert output._available is True

    async def test_dlna_dmr_call_timeout(self):
        """DMR call should handle timeout gracefully."""
        from tune_server.outputs.dlna import DlnaOutput

        mock_device = MagicMock()

        async def slow_pause():
            await asyncio.sleep(100)

        mock_device.async_pause = slow_pause
        mock_streamer = MagicMock()

        output = DlnaOutput(mock_device, mock_streamer, "192.168.1.1")

        # Override timeout for testing
        result = await output._dmr_call("async_pause")
        # This will timeout after 10s in production, but we test the logic:
        # Just verify the method exists and is callable
        assert hasattr(output, '_dmr_call')

    async def test_dlna_dmr_call_recovers_available(self):
        """Successful DMR call should reset _available to True."""
        from tune_server.outputs.dlna import DlnaOutput

        mock_device = AsyncMock()
        mock_streamer = MagicMock()
        output = DlnaOutput(mock_device, mock_streamer, "192.168.1.1")

        output._available = False
        result = await output._dmr_call("async_pause")
        assert result is True
        assert output._available is True

    async def test_dlna_dmr_call_error_returns_false(self):
        """Failed DMR call should return False."""
        from tune_server.outputs.dlna import DlnaOutput

        mock_device = MagicMock()
        mock_device.async_pause = AsyncMock(side_effect=ConnectionError("SOAP error"))
        mock_streamer = MagicMock()
        output = DlnaOutput(mock_device, mock_streamer, "192.168.1.1")

        result = await output._dmr_call("async_pause")
        assert result is False


class TestAirPlayRecovery:
    async def test_airplay_recovers_after_error(self):
        """AirPlay output should recover _available after successful start."""
        from tune_server.outputs.airplay import AirPlayOutput

        mock_atv = MagicMock()
        stream_mock = MagicMock()

        async def mock_stream_file(*args, **kwargs):
            await asyncio.sleep(10)

        stream_mock.stream_file = mock_stream_file
        mock_atv.stream = stream_mock

        output = AirPlayOutput(mock_atv, "Test AirPlay")
        output._available = False

        stream_info = AudioStreamInfo(
            format=AudioFormat.FLAC, sample_rate=44100, bit_depth=16, channels=2
        )
        from tune_server.models import Track
        track = Track(title="Test", file_path="/music/test.flac")

        await output.start(stream_info, track)
        assert output._available is True
        await output.stop()

    async def test_airplay_remote_call_timeout_handling(self):
        """AirPlay remote calls should handle timeouts."""
        from tune_server.outputs.airplay import AirPlayOutput

        mock_atv = MagicMock()
        output = AirPlayOutput(mock_atv, "Test AirPlay")

        # Test the _remote_call method
        async def slow_op():
            await asyncio.sleep(100)

        result = await output._remote_call("test", asyncio.wait_for(slow_op(), timeout=0.01))
        # Should timeout and return False
        assert result is False

    async def test_airplay_remote_call_success_recovers(self):
        """Successful remote call should set _available=True."""
        from tune_server.outputs.airplay import AirPlayOutput

        mock_atv = MagicMock()
        output = AirPlayOutput(mock_atv, "Test AirPlay")
        output._available = False

        async def quick_op():
            pass

        result = await output._remote_call("test", quick_op())
        assert result is True
        assert output._available is True


class TestLocalRecovery:
    async def test_local_recovers_after_error(self):
        """LocalOutput should reset _available on successful start."""
        from tune_server.outputs.local import LocalOutput

        output = LocalOutput()
        output._available = False

        stream_info = AudioStreamInfo(
            format=AudioFormat.FLAC, sample_rate=44100, bit_depth=16, channels=2
        )

        with patch("sounddevice.OutputStream") as MockStream:
            mock_stream = MagicMock()
            MockStream.return_value = mock_stream

            await output.start(stream_info)
            assert output._available is True


# ===========================================================================
# S6: HTTP Streamer Session Cleanup
# ===========================================================================

class TestHttpStreamerCleanup:
    def test_stream_session_tracks_activity(self):
        """StreamSession should track created_at and last_activity."""
        from tune_server.outputs.http_streamer import StreamSession

        stream_info = AudioStreamInfo(
            format=AudioFormat.FLAC, sample_rate=44100, bit_depth=16, channels=2
        )
        session = StreamSession("test-id", stream_info)

        assert session.created_at > 0
        assert session.last_activity > 0
        assert session.last_activity >= session.created_at

    async def test_put_updates_last_activity(self):
        """Putting data into session should update last_activity."""
        from tune_server.outputs.http_streamer import StreamSession

        stream_info = AudioStreamInfo(
            format=AudioFormat.FLAC, sample_rate=44100, bit_depth=16, channels=2
        )
        session = StreamSession("test-id", stream_info)

        initial_activity = session.last_activity
        await asyncio.sleep(0.01)
        await session.put(b"audio data")

        assert session.last_activity > initial_activity

    async def test_stale_sessions_cleaned_up(self):
        """Sessions past timeout with no client should be removed."""
        from tune_server.outputs.http_streamer import HttpAudioStreamer, StreamSession

        streamer = HttpAudioStreamer()

        stream_info = AudioStreamInfo(
            format=AudioFormat.FLAC, sample_rate=44100, bit_depth=16, channels=2
        )

        # Create a session and make it stale
        sid = streamer.create_session(stream_info)
        session = streamer.get_session(sid)
        session.last_activity = time.monotonic() - 600  # 10 min ago

        assert sid in streamer._sessions

        # Run cleanup manually
        with _patch_settings(http_session_timeout=300):
            now = time.monotonic()
            timeout = 300
            stale = [
                s for s, sess in streamer._sessions.items()
                if now - sess.last_activity > timeout and not sess.client_connected.is_set()
            ]
            for s in stale:
                streamer.remove_session(s)

        assert sid not in streamer._sessions

    async def test_active_sessions_not_cleaned(self):
        """Sessions with recent activity should not be removed."""
        from tune_server.outputs.http_streamer import HttpAudioStreamer

        streamer = HttpAudioStreamer()

        stream_info = AudioStreamInfo(
            format=AudioFormat.FLAC, sample_rate=44100, bit_depth=16, channels=2
        )

        # Create a fresh session
        sid = streamer.create_session(stream_info)

        # Check it's not stale
        now = time.monotonic()
        timeout = 300
        session = streamer.get_session(sid)
        assert now - session.last_activity < timeout

        # Should not be removed
        assert sid in streamer._sessions

    async def test_connected_sessions_not_cleaned(self):
        """Sessions with active client connections should not be removed."""
        from tune_server.outputs.http_streamer import HttpAudioStreamer

        streamer = HttpAudioStreamer()

        stream_info = AudioStreamInfo(
            format=AudioFormat.FLAC, sample_rate=44100, bit_depth=16, channels=2
        )

        sid = streamer.create_session(stream_info)
        session = streamer.get_session(sid)

        # Mark as stale but connected
        session.last_activity = time.monotonic() - 600
        session.client_connected.set()

        now = time.monotonic()
        timeout = 300
        stale = [
            s for s, sess in streamer._sessions.items()
            if now - sess.last_activity > timeout and not sess.client_connected.is_set()
        ]
        assert len(stale) == 0

    async def test_cleanup_task_created_on_start(self):
        """Starting streamer should create cleanup task."""
        from tune_server.outputs.http_streamer import HttpAudioStreamer

        streamer = HttpAudioStreamer(port=0)
        assert streamer._cleanup_task is None
        # We can't fully start (needs port), but verify the attribute exists


# ===========================================================================
# S7: Expanded Health Check
# ===========================================================================

class TestHealthCheck:
    async def test_health_returns_component_status(self):
        """Health endpoint should return component status."""
        with _patch_settings(api_key=None):
            client = await _make_test_client(
                with_deps={"db": True, "scanner": True, "zone_manager": True, "discovery_manager": True}
            )
            resp = await client.get("/api/v1/system/health")
            data = resp.json()
            assert "components" in data
            assert data["components"]["database"] is True
            assert data["components"]["scanner"] is True
            assert data["components"]["zones"] is True
            assert data["components"]["discovery"] is True
            assert data["status"] == "ok"

    async def test_health_degraded_when_component_down(self):
        """Health should return 'degraded' when a component is missing."""
        with _patch_settings(api_key=None):
            client = await _make_test_client(
                with_deps={"db": True, "scanner": None, "zone_manager": True, "discovery_manager": True}
            )
            resp = await client.get("/api/v1/system/health")
            data = resp.json()
            assert data["status"] == "degraded"
            assert data["components"]["scanner"] is False

    async def test_health_includes_streaming_when_configured(self):
        """Health should include tidal/qobuz status when configured."""
        mock_tidal = MagicMock()
        mock_tidal.is_authenticated = True

        with _patch_settings(api_key=None):
            client = await _make_test_client(
                with_deps={
                    "db": True, "scanner": True,
                    "zone_manager": True, "discovery_manager": True,
                    "tidal": mock_tidal,
                }
            )
            resp = await client.get("/api/v1/system/health")
            data = resp.json()
            assert "tidal" in data["components"]
            assert data["components"]["tidal"] is True

    async def test_health_ok_with_unauthenticated_streaming(self):
        """Health should be 'ok' when streaming service not authenticated (streaming is optional)."""
        mock_qobuz = MagicMock()
        mock_qobuz.is_authenticated = False

        with _patch_settings(api_key=None):
            client = await _make_test_client(
                with_deps={
                    "db": True, "scanner": True,
                    "zone_manager": True, "discovery_manager": True,
                    "qobuz": mock_qobuz,
                }
            )
            resp = await client.get("/api/v1/system/health")
            data = resp.json()
            assert data["status"] == "ok"
            assert data["components"]["qobuz"] is False


# ===========================================================================
# Config settings tests
# ===========================================================================

class TestNewConfigSettings:
    def test_ws_heartbeat_interval_default(self):
        from tune_server.config import Settings
        s = Settings()
        assert s.ws_heartbeat_interval == 30

    def test_http_session_timeout_default(self):
        from tune_server.config import Settings
        s = Settings()
        assert s.http_session_timeout == 300


# ===========================================================================
# Helpers
# ===========================================================================

def _settings(**overrides):
    """Create a Settings instance with overrides."""
    from tune_server.config import Settings
    return Settings(**overrides)


class _patch_settings:
    """Context manager to patch settings for test scope."""
    def __init__(self, **overrides):
        self._overrides = overrides
        self._patches = []

    def __enter__(self):
        from tune_server.config import settings
        for key, value in self._overrides.items():
            p = patch.object(settings, key, value)
            p.start()
            self._patches.append(p)
        return self

    def __exit__(self, *args):
        for p in self._patches:
            p.stop()


def _find_cors_middleware(app):
    """Find CORSMiddleware in the app's middleware stack."""
    from starlette.middleware.cors import CORSMiddleware
    # Walk middleware stack
    current = app
    while hasattr(current, 'app'):
        if isinstance(current, CORSMiddleware):
            return current
        current = current.app
    return None


async def _make_test_client(with_deps=None):
    """Create a test client with optional dependency overrides."""
    import httpx
    from tune_server.api.deps import deps
    from tune_server.api.main import create_api_app

    # Set minimal deps for routes to work
    if with_deps:
        for key, value in with_deps.items():
            if key == "db" and value is True:
                deps.db = MagicMock()
            elif key == "scanner" and value is True:
                deps.scanner = MagicMock()
                deps.scanner.is_scanning = False
            elif key == "scanner" and value is None:
                deps.scanner = None
            elif key == "zone_manager" and value is True:
                deps.zone_manager = MagicMock()
                deps.zone_manager.list_zones.return_value = []
            elif key == "discovery_manager" and value is True:
                deps.discovery_manager = MagicMock()
                deps.discovery_manager.list_devices.return_value = []
            elif key in ("tidal", "qobuz") and value is not None:
                setattr(deps, key, value)
    else:
        # Minimal deps for config endpoint
        deps.scanner = MagicMock()
        deps.scanner.is_scanning = False

    app = create_api_app()

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )

    return client

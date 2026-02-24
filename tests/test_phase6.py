"""Phase 6 tests — Federated search, WebSocket filtering, playlist sync, polish."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import (
    FederatedSearchResult,
    SearchResult,
    Source,
    SystemStatsResponse,
    Track,
    WsSubscribeMessage,
)


# =========================================================================
# A1 — Federated Search
# =========================================================================


class TestFederatedSearch:

    @pytest.mark.asyncio
    async def test_federated_search_local_only(self, app_client):
        """No streaming services — returns local results only."""
        from tune_server.api.deps import deps
        # Ensure no leftover streaming services from other tests
        saved = dict(deps.streaming_services)
        deps.streaming_services.clear()
        try:
            resp = await app_client.get("/api/v1/search?q=nuit")
            assert resp.status_code == 200
            data = resp.json()
            assert "local" in data
            assert "services" in data
            # Should find "La nuit je mens" in local results
            assert len(data["local"]["tracks"]) >= 1
            assert data["services"] == {}
        finally:
            deps.streaming_services.update(saved)

    @pytest.mark.asyncio
    async def test_federated_search_with_streaming(self, app_client):
        """Mock streaming service returns results in services dict."""
        from tune_server.api.deps import deps

        mock_service = AsyncMock()
        type(mock_service).is_authenticated = PropertyMock(return_value=True)
        mock_service.search.return_value = SearchResult(
            tracks=[Track(title="Stream Track", source=Source.TIDAL, source_id="s1")],
            albums=[],
            artists=[],
        )
        deps.streaming_services["tidal"] = mock_service

        try:
            resp = await app_client.get("/api/v1/search?q=test")
            assert resp.status_code == 200
            data = resp.json()
            assert "tidal" in data["services"]
            assert data["services"]["tidal"]["tracks"][0]["title"] == "Stream Track"
        finally:
            deps.streaming_services.pop("tidal", None)

    @pytest.mark.asyncio
    async def test_federated_search_service_failure_graceful(self, app_client):
        """One service raises, others still return results."""
        from tune_server.api.deps import deps

        failing_service = AsyncMock()
        type(failing_service).is_authenticated = PropertyMock(return_value=True)
        failing_service.search.side_effect = RuntimeError("Service down")

        working_service = AsyncMock()
        type(working_service).is_authenticated = PropertyMock(return_value=True)
        working_service.search.return_value = SearchResult(
            tracks=[Track(title="Working", source=Source.QOBUZ, source_id="w1")],
            albums=[],
            artists=[],
        )

        deps.streaming_services["bad_svc"] = failing_service
        deps.streaming_services["good_svc"] = working_service

        try:
            resp = await app_client.get("/api/v1/search?q=test")
            assert resp.status_code == 200
            data = resp.json()
            # Failing service should not appear in results
            assert "bad_svc" not in data["services"]
            # Working service should return results
            assert "good_svc" in data["services"]
            assert data["services"]["good_svc"]["tracks"][0]["title"] == "Working"
        finally:
            deps.streaming_services.pop("bad_svc", None)
            deps.streaming_services.pop("good_svc", None)

    @pytest.mark.asyncio
    async def test_federated_search_sources_filter(self, app_client):
        """?sources=local,tidal only queries those services."""
        from tune_server.api.deps import deps

        tidal = AsyncMock()
        type(tidal).is_authenticated = PropertyMock(return_value=True)
        tidal.search.return_value = SearchResult(tracks=[], albums=[], artists=[])

        youtube = AsyncMock()
        type(youtube).is_authenticated = PropertyMock(return_value=True)
        youtube.search.return_value = SearchResult(tracks=[], albums=[], artists=[])

        deps.streaming_services["tidal"] = tidal
        deps.streaming_services["youtube"] = youtube

        try:
            resp = await app_client.get("/api/v1/search?q=test&sources=local,tidal")
            assert resp.status_code == 200
            # Tidal was queried
            tidal.search.assert_called_once()
            # YouTube was NOT queried
            youtube.search.assert_not_called()
        finally:
            deps.streaming_services.pop("tidal", None)
            deps.streaming_services.pop("youtube", None)

    @pytest.mark.asyncio
    async def test_federated_search_empty_query_rejected(self, app_client):
        """Empty q returns 422."""
        resp = await app_client.get("/api/v1/search?q=")
        assert resp.status_code == 422


# =========================================================================
# A2 — WebSocket Event Filtering
# =========================================================================


class TestWebSocketFiltering:

    @pytest.mark.asyncio
    async def test_ws_default_receives_all_events(self):
        """No subscribe message — client gets everything (backward compatible)."""
        event_bus = EventBus()
        from tune_server.api.websocket import WebSocketManager
        mgr = WebSocketManager(event_bus)
        await mgr.start()

        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_text = AsyncMock()

        await mgr.connect(ws)

        # Emit two different event types
        await event_bus.emit(Event(type=EventType.PLAYBACK_STARTED, data={"zone_id": 1}))
        await event_bus.emit(Event(type=EventType.ZONE_CREATED, data={"zone_id": 2}))

        assert ws.send_text.call_count == 2

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_ws_subscribe_filters_events(self):
        """Subscribe to playback.* only gets playback events."""
        event_bus = EventBus()
        from tune_server.api.websocket import WebSocketManager
        mgr = WebSocketManager(event_bus)
        await mgr.start()

        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_text = AsyncMock()

        await mgr.connect(ws)

        # Subscribe to playback events only
        mgr._subscriptions[ws] = {"playback.*"}

        await event_bus.emit(Event(type=EventType.PLAYBACK_STARTED, data={}))
        await event_bus.emit(Event(type=EventType.ZONE_CREATED, data={}))
        await event_bus.emit(Event(type=EventType.PLAYBACK_PAUSED, data={}))

        # Should only receive 2 playback events, not zone event
        assert ws.send_text.call_count == 2

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_ws_subscribe_wildcard(self):
        """Subscribe to * gets everything."""
        event_bus = EventBus()
        from tune_server.api.websocket import WebSocketManager
        mgr = WebSocketManager(event_bus)
        await mgr.start()

        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_text = AsyncMock()

        await mgr.connect(ws)

        # Explicit wildcard
        mgr._subscriptions[ws] = {"*"}

        await event_bus.emit(Event(type=EventType.PLAYBACK_STARTED, data={}))
        await event_bus.emit(Event(type=EventType.ZONE_CREATED, data={}))

        assert ws.send_text.call_count == 2

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_ws_unsubscribe_gets_nothing(self):
        """action=unsubscribe stops all events."""
        event_bus = EventBus()
        from tune_server.api.websocket import WebSocketManager
        mgr = WebSocketManager(event_bus)
        await mgr.start()

        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_text = AsyncMock()

        await mgr.connect(ws)

        # Unsubscribe = empty set = receive nothing
        mgr._subscriptions[ws] = set()

        await event_bus.emit(Event(type=EventType.PLAYBACK_STARTED, data={}))
        await event_bus.emit(Event(type=EventType.ZONE_CREATED, data={}))

        assert ws.send_text.call_count == 0

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_ws_multiple_patterns(self):
        """Subscribe to multiple patterns combines them."""
        event_bus = EventBus()
        from tune_server.api.websocket import WebSocketManager
        mgr = WebSocketManager(event_bus)
        await mgr.start()

        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_text = AsyncMock()

        await mgr.connect(ws)
        mgr._subscriptions[ws] = {"playback.*", "zone.*"}

        await event_bus.emit(Event(type=EventType.PLAYBACK_STARTED, data={}))
        await event_bus.emit(Event(type=EventType.ZONE_CREATED, data={}))
        await event_bus.emit(Event(type=EventType.DEVICE_DISCOVERED, data={}))

        # Should get playback + zone but not device
        assert ws.send_text.call_count == 2

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_ws_pattern_no_match_skipped(self):
        """Subscribe to library.* doesn't get playback.started."""
        event_bus = EventBus()
        from tune_server.api.websocket import WebSocketManager
        mgr = WebSocketManager(event_bus)
        await mgr.start()

        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_text = AsyncMock()

        await mgr.connect(ws)
        mgr._subscriptions[ws] = {"library.*"}

        await event_bus.emit(Event(type=EventType.PLAYBACK_STARTED, data={}))
        await event_bus.emit(Event(type=EventType.ZONE_CREATED, data={}))

        assert ws.send_text.call_count == 0

        await mgr.stop()


# =========================================================================
# A3 — Playlist Sync Events
# =========================================================================


class TestPlaylistSyncEvents:

    @pytest.mark.asyncio
    async def test_playlist_create_emits_event(self, app_client, event_bus):
        """POST /playlists emits PLAYLIST_CREATED."""
        events = []
        event_bus.on(EventType.PLAYLIST_CREATED, lambda e: events.append(e))

        resp = await app_client.post("/api/v1/playlists", json={"name": "My Playlist"})
        assert resp.status_code == 201

        # Let the emit_nowait task run
        await asyncio.sleep(0.1)

        assert len(events) == 1
        assert events[0].type == EventType.PLAYLIST_CREATED
        assert events[0].data["name"] == "My Playlist"
        assert events[0].source == "playlists"

    @pytest.mark.asyncio
    async def test_playlist_update_emits_event(self, app_client, event_bus):
        """PUT /playlists/{id} emits PLAYLIST_UPDATED."""
        # Create a playlist first
        resp = await app_client.post("/api/v1/playlists", json={"name": "Original"})
        playlist_id = resp.json()["id"]

        events = []
        event_bus.on(EventType.PLAYLIST_UPDATED, lambda e: events.append(e))

        resp = await app_client.put(
            f"/api/v1/playlists/{playlist_id}",
            json={"name": "Renamed"},
        )
        assert resp.status_code == 200

        await asyncio.sleep(0.1)

        assert len(events) == 1
        assert events[0].type == EventType.PLAYLIST_UPDATED
        assert events[0].data["playlist_id"] == playlist_id
        assert events[0].source == "playlists"

    @pytest.mark.asyncio
    async def test_playlist_delete_emits_event(self, app_client, event_bus):
        """DELETE /playlists/{id} emits PLAYLIST_DELETED."""
        resp = await app_client.post("/api/v1/playlists", json={"name": "To Delete"})
        playlist_id = resp.json()["id"]

        events = []
        event_bus.on(EventType.PLAYLIST_DELETED, lambda e: events.append(e))

        resp = await app_client.delete(f"/api/v1/playlists/{playlist_id}")
        assert resp.status_code == 204

        await asyncio.sleep(0.1)

        assert len(events) == 1
        assert events[0].type == EventType.PLAYLIST_DELETED
        assert events[0].data["playlist_id"] == playlist_id

    @pytest.mark.asyncio
    async def test_playlist_add_tracks_emits_event(self, app_client, event_bus, sample_tracks):
        """POST /playlists/{id}/tracks emits PLAYLIST_TRACKS_CHANGED."""
        resp = await app_client.post("/api/v1/playlists", json={"name": "Track List"})
        playlist_id = resp.json()["id"]

        events = []
        event_bus.on(EventType.PLAYLIST_TRACKS_CHANGED, lambda e: events.append(e))

        resp = await app_client.post(
            f"/api/v1/playlists/{playlist_id}/tracks",
            json={"track_ids": [sample_tracks[0].id]},
        )
        assert resp.status_code == 200

        await asyncio.sleep(0.1)

        assert len(events) == 1
        assert events[0].type == EventType.PLAYLIST_TRACKS_CHANGED
        assert events[0].data["playlist_id"] == playlist_id

    @pytest.mark.asyncio
    async def test_playlist_remove_track_emits_event(self, app_client, event_bus, sample_tracks):
        """DELETE /playlists/{id}/tracks/{tid} emits PLAYLIST_TRACKS_CHANGED."""
        resp = await app_client.post("/api/v1/playlists", json={"name": "Remove Test"})
        playlist_id = resp.json()["id"]

        # Add a track first
        await app_client.post(
            f"/api/v1/playlists/{playlist_id}/tracks",
            json={"track_ids": [sample_tracks[0].id]},
        )
        await asyncio.sleep(0.05)

        events = []
        event_bus.on(EventType.PLAYLIST_TRACKS_CHANGED, lambda e: events.append(e))

        resp = await app_client.delete(
            f"/api/v1/playlists/{playlist_id}/tracks/{sample_tracks[0].id}"
        )
        assert resp.status_code == 204

        await asyncio.sleep(0.1)

        assert len(events) == 1
        assert events[0].type == EventType.PLAYLIST_TRACKS_CHANGED

    @pytest.mark.asyncio
    async def test_playlist_reorder_emits_event(self, app_client, event_bus, sample_tracks):
        """PUT /playlists/{id}/tracks emits PLAYLIST_TRACKS_CHANGED."""
        resp = await app_client.post("/api/v1/playlists", json={"name": "Reorder Test"})
        playlist_id = resp.json()["id"]

        track_ids = [t.id for t in sample_tracks[:2]]
        await app_client.post(
            f"/api/v1/playlists/{playlist_id}/tracks",
            json={"track_ids": track_ids},
        )
        await asyncio.sleep(0.05)

        events = []
        event_bus.on(EventType.PLAYLIST_TRACKS_CHANGED, lambda e: events.append(e))

        resp = await app_client.put(
            f"/api/v1/playlists/{playlist_id}/tracks",
            json={"track_ids": list(reversed(track_ids))},
        )
        assert resp.status_code == 200

        await asyncio.sleep(0.1)

        assert len(events) == 1
        assert events[0].type == EventType.PLAYLIST_TRACKS_CHANGED


# =========================================================================
# B — Low-Severity Fixes
# =========================================================================


class TestSystemStatsResponse:

    @pytest.mark.asyncio
    async def test_system_stats_returns_typed_response(self, app_client):
        """Verify /system/stats returns a response matching SystemStatsResponse."""
        resp = await app_client.get("/api/v1/system/stats")
        assert resp.status_code == 200
        data = resp.json()
        # Validate it can be parsed as SystemStatsResponse
        stats = SystemStatsResponse(**data)
        assert isinstance(stats.tracks, int)
        assert isinstance(stats.albums, int)
        assert isinstance(stats.artists, int)
        assert isinstance(stats.zones, int)
        assert isinstance(stats.devices, int)


class TestCacheLRUEviction:

    def test_cache_lru_eviction(self):
        """Set max_size=2, add 3 entries, first evicted."""
        from tune_server.streaming.cache import StreamUrlCache

        cache = StreamUrlCache(ttl_seconds=300, max_size=2)
        cache.set("a", "url_a")
        cache.set("b", "url_b")
        cache.set("c", "url_c")

        # "a" should be evicted
        assert cache.get("a") is None
        assert cache.get("b") == "url_b"
        assert cache.get("c") == "url_c"

    def test_cache_lru_preserves_recent(self):
        """Eviction removes oldest, keeps newest."""
        from tune_server.streaming.cache import StreamUrlCache

        cache = StreamUrlCache(ttl_seconds=300, max_size=3)
        cache.set("1", "url_1")
        cache.set("2", "url_2")
        cache.set("3", "url_3")

        # All present
        assert cache.get("1") == "url_1"
        assert cache.get("2") == "url_2"
        assert cache.get("3") == "url_3"

        # Add a 4th — should evict "1" (oldest)
        cache.set("4", "url_4")
        assert cache.get("1") is None
        assert cache.get("2") == "url_2"
        assert cache.get("4") == "url_4"


class TestOffsetValidation:

    @pytest.mark.asyncio
    async def test_offset_validation_rejects_negative(self, app_client):
        """offset=-1 returns 422 for tracks, albums, and artists."""
        for endpoint in ["/api/v1/library/tracks", "/api/v1/library/albums", "/api/v1/library/artists"]:
            resp = await app_client.get(f"{endpoint}?offset=-1")
            assert resp.status_code == 422, f"Expected 422 for {endpoint} with offset=-1"


# =========================================================================
# Model tests
# =========================================================================


class TestNewModels:

    def test_federated_search_result_defaults(self):
        result = FederatedSearchResult()
        assert result.local == SearchResult()
        assert result.services == {}

    def test_federated_search_result_with_services(self):
        local = SearchResult(tracks=[Track(title="Local", source=Source.LOCAL)])
        tidal = SearchResult(tracks=[Track(title="Tidal", source=Source.TIDAL, source_id="t1")])
        result = FederatedSearchResult(local=local, services={"tidal": tidal})
        assert len(result.local.tracks) == 1
        assert len(result.services["tidal"].tracks) == 1

    def test_system_stats_response(self):
        stats = SystemStatsResponse(tracks=100, albums=10, artists=5, zones=2, devices=3)
        assert stats.tracks == 100
        assert stats.zones == 2
        d = stats.model_dump()
        assert d == {"tracks": 100, "albums": 10, "artists": 5, "zones": 2, "devices": 3}

    def test_ws_subscribe_message(self):
        msg = WsSubscribeMessage(action="subscribe", patterns=["playback.*"])
        assert msg.action == "subscribe"
        assert msg.patterns == ["playback.*"]


class TestPlaylistEventTypes:

    def test_playlist_event_types_exist(self):
        assert EventType.PLAYLIST_CREATED.value == "playlist.created"
        assert EventType.PLAYLIST_UPDATED.value == "playlist.updated"
        assert EventType.PLAYLIST_DELETED.value == "playlist.deleted"
        assert EventType.PLAYLIST_TRACKS_CHANGED.value == "playlist.tracks_changed"

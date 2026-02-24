from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.api.websocket import WebSocketManager


@pytest.fixture
def ws_manager(event_bus):
    return WebSocketManager(event_bus)


def _mock_websocket():
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


async def test_connect(ws_manager):
    ws = _mock_websocket()
    await ws_manager.connect(ws)
    ws.accept.assert_awaited_once()
    assert ws in ws_manager._subscriptions


async def test_disconnect(ws_manager):
    ws = _mock_websocket()
    await ws_manager.connect(ws)
    await ws_manager.disconnect(ws)
    assert ws not in ws_manager._subscriptions


async def test_on_event_broadcasts(ws_manager, event_bus):
    ws1 = _mock_websocket()
    ws2 = _mock_websocket()
    await ws_manager.connect(ws1)
    await ws_manager.connect(ws2)

    event = Event(type=EventType.PLAYBACK_STARTED, data={"zone_id": 1}, source="player")
    await ws_manager._on_event(event)

    ws1.send_text.assert_awaited_once()
    ws2.send_text.assert_awaited_once()

    # Verify the message is valid JSON with expected fields
    msg = json.loads(ws1.send_text.call_args[0][0])
    assert msg["type"] == "playback.started"
    assert msg["data"]["zone_id"] == 1
    assert msg["source"] == "player"


async def test_on_event_no_connections(ws_manager):
    event = Event(type=EventType.PLAYBACK_STARTED, data={}, source="player")
    # Should not raise
    await ws_manager._on_event(event)


async def test_on_event_dead_connection(ws_manager):
    ws_good = _mock_websocket()
    ws_dead = _mock_websocket()
    ws_dead.send_text.side_effect = ConnectionError("closed")

    await ws_manager.connect(ws_good)
    await ws_manager.connect(ws_dead)

    event = Event(type=EventType.PLAYBACK_STARTED, data={}, source="player")
    await ws_manager._on_event(event)

    # Dead connection should be removed
    assert ws_dead not in ws_manager._subscriptions
    # Good connection should still be there
    assert ws_good in ws_manager._subscriptions


async def test_start_subscribes(ws_manager, event_bus):
    await ws_manager.start()
    assert ws_manager._unsubscribe is not None
    # Verify it's callable (the unsubscribe function)
    assert callable(ws_manager._unsubscribe)

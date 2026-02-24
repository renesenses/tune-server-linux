from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from tune_server.event_bus import Event, EventBus, EventType


class TestEmit:
    async def test_emit_calls_listener(self, event_bus: EventBus):
        listener = AsyncMock()
        event_bus.on(EventType.PLAYBACK_STARTED, listener)

        event = Event(type=EventType.PLAYBACK_STARTED, data={"zone_id": 1})
        await event_bus.emit(event)

        listener.assert_awaited_once_with(event)

    async def test_emit_multiple_listeners(self, event_bus: EventBus):
        listener1 = AsyncMock()
        listener2 = AsyncMock()
        event_bus.on(EventType.PLAYBACK_STARTED, listener1)
        event_bus.on(EventType.PLAYBACK_STARTED, listener2)

        event = Event(type=EventType.PLAYBACK_STARTED)
        await event_bus.emit(event)

        listener1.assert_awaited_once_with(event)
        listener2.assert_awaited_once_with(event)

    async def test_emit_wrong_type_not_called(self, event_bus: EventBus):
        listener = AsyncMock()
        event_bus.on(EventType.PLAYBACK_STARTED, listener)

        await event_bus.emit(Event(type=EventType.ZONE_CREATED))

        listener.assert_not_awaited()

    async def test_no_listeners(self, event_bus: EventBus):
        # Should not raise
        await event_bus.emit(Event(type=EventType.PLAYBACK_STARTED))


class TestOnAll:
    async def test_on_all_receives_everything(self, event_bus: EventBus):
        listener = AsyncMock()
        event_bus.on_all(listener)

        e1 = Event(type=EventType.PLAYBACK_STARTED)
        e2 = Event(type=EventType.ZONE_CREATED)
        await event_bus.emit(e1)
        await event_bus.emit(e2)

        assert listener.await_count == 2
        listener.assert_any_await(e1)
        listener.assert_any_await(e2)

    async def test_unsubscribe_on_all(self, event_bus: EventBus):
        listener = AsyncMock()
        unsub = event_bus.on_all(listener)

        await event_bus.emit(Event(type=EventType.PLAYBACK_STARTED))
        assert listener.await_count == 1

        unsub()
        await event_bus.emit(Event(type=EventType.ZONE_CREATED))
        assert listener.await_count == 1


class TestUnsubscribe:
    async def test_unsubscribe(self, event_bus: EventBus):
        listener = AsyncMock()
        unsub = event_bus.on(EventType.PLAYBACK_STARTED, listener)

        await event_bus.emit(Event(type=EventType.PLAYBACK_STARTED))
        assert listener.await_count == 1

        unsub()
        await event_bus.emit(Event(type=EventType.PLAYBACK_STARTED))
        assert listener.await_count == 1  # not called again


class TestErrorIsolation:
    async def test_listener_error_isolated(self, event_bus: EventBus):
        async def bad_listener(event: Event):
            raise RuntimeError("boom")

        good_listener = AsyncMock()

        event_bus.on(EventType.PLAYBACK_STARTED, bad_listener)
        event_bus.on(EventType.PLAYBACK_STARTED, good_listener)

        await event_bus.emit(Event(type=EventType.PLAYBACK_STARTED))

        good_listener.assert_awaited_once()


class TestEmitNowait:
    async def test_emit_nowait(self, event_bus: EventBus):
        listener = AsyncMock()
        event_bus.on(EventType.PLAYBACK_STARTED, listener)

        event_bus.emit_nowait(Event(type=EventType.PLAYBACK_STARTED))

        # Give the background task time to run
        await asyncio.sleep(0.05)

        listener.assert_awaited_once()


class TestEventData:
    async def test_event_data(self, event_bus: EventBus):
        captured = []

        async def capture(event: Event):
            captured.append(event)

        event_bus.on(EventType.PLAYBACK_STARTED, capture)

        await event_bus.emit(
            Event(
                type=EventType.PLAYBACK_STARTED,
                data={"track_id": 42},
                source="test",
            )
        )

        assert len(captured) == 1
        assert captured[0].type == EventType.PLAYBACK_STARTED
        assert captured[0].data == {"track_id": 42}
        assert captured[0].source == "test"

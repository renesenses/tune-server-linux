from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

import structlog

logger = structlog.get_logger()


class EventType(str, Enum):
    # Library events
    LIBRARY_SCAN_STARTED = "library.scan.started"
    LIBRARY_SCAN_PROGRESS = "library.scan.progress"
    LIBRARY_SCAN_COMPLETED = "library.scan.completed"
    LIBRARY_TRACK_ADDED = "library.track.added"
    LIBRARY_TRACK_UPDATED = "library.track.updated"
    LIBRARY_TRACK_REMOVED = "library.track.removed"
    LIBRARY_ARTWORK_PROGRESS = "library.artwork.progress"
    LIBRARY_ARTWORK_COMPLETED = "library.artwork.completed"

    # Playback events
    PLAYBACK_STARTED = "playback.started"
    PLAYBACK_PAUSED = "playback.paused"
    PLAYBACK_RESUMED = "playback.resumed"
    PLAYBACK_STOPPED = "playback.stopped"
    PLAYBACK_TRACK_CHANGED = "playback.track_changed"
    PLAYBACK_POSITION = "playback.position"
    PLAYBACK_ERROR = "playback.error"
    PLAYBACK_METADATA = "playback.metadata"
    PLAYBACK_QUEUE_CHANGED = "playback.queue_changed"

    # Playlist events
    PLAYLIST_CREATED = "playlist.created"
    PLAYLIST_UPDATED = "playlist.updated"
    PLAYLIST_DELETED = "playlist.deleted"
    PLAYLIST_TRACKS_CHANGED = "playlist.tracks_changed"

    # Zone events
    ZONE_CREATED = "zone.created"
    ZONE_DELETED = "zone.deleted"
    ZONE_UPDATED = "zone.updated"
    ZONE_GROUPED = "zone.grouped"
    ZONE_UNGROUPED = "zone.ungrouped"
    ZONE_VOLUME_CHANGED = "zone.volume_changed"

    # Device events
    DEVICE_DISCOVERED = "device.discovered"
    DEVICE_LOST = "device.lost"
    DEVICE_UPDATED = "device.updated"

    # Network events
    NETWORK_SHARE_DISCOVERED = "network.share.discovered"
    NETWORK_SHARE_LOST = "network.share.lost"
    NETWORK_MOUNT_ADDED = "network.mount.added"
    NETWORK_MOUNT_REMOVED = "network.mount.removed"
    MEDIA_SERVER_DISCOVERED = "network.media_server.discovered"
    MEDIA_SERVER_LOST = "network.media_server.lost"

    # Radio events
    RADIO_CREATED = "radio.created"
    RADIO_UPDATED = "radio.updated"
    RADIO_DELETED = "radio.deleted"

    # System events
    SYSTEM_STARTED = "system.started"
    SYSTEM_STOPPING = "system.stopping"


@dataclass
class Event:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""


Listener = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    def __init__(self) -> None:
        self._listeners: dict[EventType, list[Listener]] = defaultdict(list)
        self._global_listeners: list[Listener] = []

    def on(self, event_type: EventType, listener: Listener) -> Callable[[], None]:
        self._listeners[event_type].append(listener)

        def unsubscribe() -> None:
            self._listeners[event_type].remove(listener)

        return unsubscribe

    def on_all(self, listener: Listener) -> Callable[[], None]:
        self._global_listeners.append(listener)

        def unsubscribe() -> None:
            self._global_listeners.remove(listener)

        return unsubscribe

    async def emit(self, event: Event) -> None:
        listeners = list(self._listeners.get(event.type, []))
        listeners.extend(self._global_listeners)

        for listener in listeners:
            try:
                await listener(event)
            except Exception:
                logger.exception(
                    "event_listener_error",
                    event_type=event.type,
                    listener=listener.__qualname__,
                )

    def emit_nowait(self, event: Event) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("emit_nowait_no_loop", event_type=event.type)
            return
        task = loop.create_task(self.emit(event))
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("emit_nowait_error", error=str(exc))

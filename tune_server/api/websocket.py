from __future__ import annotations

import asyncio
import json
from fnmatch import fnmatch

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from tune_server.config import settings
from tune_server.event_bus import Event, EventBus

logger = structlog.get_logger()

MAX_WS_CONNECTIONS = 50


class WebSocketManager:
    """Manages WebSocket connections and broadcasts events to clients."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._subscriptions: dict[WebSocket, set[str]] = {}
        self._unsubscribe = None

    async def start(self) -> None:
        self._unsubscribe = self._event_bus.on_all(self._on_event)

    async def stop(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()

        for ws in list(self._subscriptions):
            try:
                await ws.close()
            except Exception:
                pass
        self._subscriptions.clear()

    async def connect(self, websocket: WebSocket) -> bool:
        if len(self._subscriptions) >= MAX_WS_CONNECTIONS:
            await websocket.close(code=1013, reason="Too many connections")
            return False
        await websocket.accept()
        self._subscriptions[websocket] = {"*"}  # Default: receive all events
        logger.info("websocket_connected", count=len(self._subscriptions))
        return True

    async def disconnect(self, websocket: WebSocket) -> None:
        self._subscriptions.pop(websocket, None)
        logger.info("websocket_disconnected", count=len(self._subscriptions))

    async def _on_event(self, event: Event) -> None:
        if not self._subscriptions:
            return

        event_type = event.type.value
        message = json.dumps({
            "type": event_type,
            "data": event.data,
            "source": event.source,
        })

        dead: list[WebSocket] = []
        for ws, patterns in list(self._subscriptions.items()):
            if any(fnmatch(event_type, p) for p in patterns):
                try:
                    await ws.send_text(message)
                except Exception:
                    dead.append(ws)

        for ws in dead:
            self._subscriptions.pop(ws, None)

    async def handle_websocket(self, websocket: WebSocket) -> None:
        connected = await self.connect(websocket)
        if not connected:
            return
        try:
            while True:
                try:
                    timeout = settings.ws_heartbeat_interval or None
                    data = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=timeout,
                    )
                    if data == "pong":
                        continue
                    # Try to parse as JSON for subscribe/unsubscribe
                    try:
                        msg = json.loads(data)
                        if isinstance(msg, dict):
                            action = msg.get("action")
                            if action == "subscribe":
                                self._subscriptions[websocket] = set(msg.get("patterns", ["*"]))
                            elif action == "unsubscribe":
                                self._subscriptions[websocket] = set()
                    except (json.JSONDecodeError, TypeError):
                        pass
                except asyncio.TimeoutError:
                    # No message received — send ping
                    try:
                        await websocket.send_text('{"type":"ping"}')
                    except Exception:
                        break  # Connection dead
        except WebSocketDisconnect:
            pass
        finally:
            await self.disconnect(websocket)

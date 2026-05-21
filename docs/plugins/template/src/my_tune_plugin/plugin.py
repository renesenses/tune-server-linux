"""Example Tune plugin — demonstrates routes, event listeners, and player hooks.

Rename, edit, and extend to build your own plugin.
"""
from __future__ import annotations

from typing import Optional

import structlog
from pydantic_settings import BaseSettings

from tune_server.event_bus import Event, EventType
from tune_server.playback.player import PlayerHookEvent
from tune_server.plugins.api import PluginContext

from my_tune_plugin.routes import router

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Configuration (environment: TUNE_EXAMPLE_*)
# ---------------------------------------------------------------------------


class ExampleSettings(BaseSettings):
    """Plugin settings loaded from TUNE_EXAMPLE_* env vars."""

    enabled: bool = True
    greeting: str = "Hello from the example plugin!"

    model_config = {"env_prefix": "TUNE_EXAMPLE_"}


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class ExamplePlugin:
    """A minimal but complete Tune plugin.

    Registers:
      - An API route at GET /api/v1/example/hello
      - A player hook that logs track starts
      - An event listener on library scan completion
    """

    name = "example"
    version = "0.1.0"
    description = "Example plugin — routes, hooks, events"

    def __init__(self) -> None:
        self._settings = ExampleSettings()
        self._unsubs: list = []

    async def setup(self, ctx: PluginContext) -> None:
        if not self._settings.enabled:
            logger.info("example_plugin_disabled")
            return

        # 1. Register API routes
        ctx.register_router(router)

        # 2. Register a player hook (fires for ALL zones)
        ctx.register_player_hook(
            PlayerHookEvent.BEFORE_TRACK,
            self._on_before_track,
        )

        # 3. Subscribe to event bus
        self._unsubs.append(
            ctx.event_bus.on(
                EventType.LIBRARY_SCAN_COMPLETED,
                self._on_scan_complete,
            )
        )

        logger.info(
            "example_plugin_ready",
            greeting=self._settings.greeting,
        )

    async def _on_before_track(self, zone_id: int, track) -> None:
        """Fires just before audio starts for every track in every zone."""
        logger.debug(
            "example_before_track",
            zone_id=zone_id,
            title=getattr(track, "title", "?"),
        )

    async def _on_scan_complete(self, event: Event) -> None:
        """Fires when a library scan finishes."""
        logger.info(
            "example_scan_complete",
            tracks_added=event.data.get("tracks_added", 0),
        )

    async def teardown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        logger.info("example_plugin_stopped")

    def config_schema(self) -> Optional[type[BaseSettings]]:
        return ExampleSettings

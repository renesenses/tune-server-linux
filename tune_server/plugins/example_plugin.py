"""Example Tune Server plugin — tutorial for third-party developers.

This minimal plugin demonstrates every hook point in the Tune Plugin SDK:
  - Registering an API route
  - Registering a player hook
  - Subscribing to EventBus events
  - Declaring a config schema via pydantic-settings
  - Proper setup/teardown lifecycle

To install this plugin for testing, add it to your pyproject.toml::

    [project.entry-points."tune_server.plugins"]
    hello = "tune_server.plugins.example_plugin:HelloPlugin"

Then restart the server. The plugin registers:
  - GET /api/v1/hello  — returns a greeting message
  - A player hook that logs every track start
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter
from pydantic_settings import BaseSettings

from tune_server.plugins.api import PROTOCOL_VERSION, PluginContext, TunePlugin
from tune_server.playback.player import PlayerHookEvent

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# 1. Configuration — pydantic-settings with TUNE_HELLO_* env prefix
# ---------------------------------------------------------------------------

class HelloSettings(BaseSettings):
    """Plugin settings, loaded from environment variables.

    All variables use the prefix ``TUNE_HELLO_``. For example, set
    ``TUNE_HELLO_GREETING=Bonjour`` in ``.env`` or the shell.
    """

    greeting: str = "Hello from example plugin!"
    """The message returned by GET /api/v1/hello."""

    model_config = {"env_prefix": "TUNE_HELLO_"}


# ---------------------------------------------------------------------------
# 2. API router — mounted during setup()
# ---------------------------------------------------------------------------

hello_router = APIRouter(tags=["hello-plugin"])


@hello_router.get("/hello")
async def hello():
    """Greeting endpoint contributed by the Hello plugin."""
    # Access plugin-level state through the module-level _settings reference.
    return {"message": _settings.greeting}


# Module-level ref so the route handler can read config after setup().
_settings = HelloSettings()


# ---------------------------------------------------------------------------
# 3. Plugin class
# ---------------------------------------------------------------------------

class HelloPlugin:
    """Minimal example plugin satisfying the TunePlugin protocol.

    Lifecycle:
        1. ``__init__()`` — no work here; class is instantiated by the loader
        2. ``setup(ctx)`` — register routes, hooks, event listeners
        3. Live — the plugin is active until the server shuts down
        4. ``teardown()`` — clean up resources (unsubscribe, close connections)
    """

    name: str = "hello"
    version: str = "0.1.0"
    description: str = "Example plugin — greeting endpoint and track logger"

    def __init__(self) -> None:
        self._unsub_event: Optional[callable] = None

    async def setup(self, ctx: PluginContext) -> None:
        """Called once after core init, before zones spawn."""
        global _settings
        _settings = HelloSettings()

        logger.info(
            "hello_plugin_setup",
            greeting=_settings.greeting,
            protocol_version=PROTOCOL_VERSION,
        )

        # Register an API router — all routes are prefixed with /api/v1
        ctx.register_router(hello_router)

        # Register a player hook — fires on every track start in every zone
        ctx.register_player_hook(PlayerHookEvent.BEFORE_TRACK, self._on_track_start)

        # Subscribe to EventBus events — custom string event types work too
        from tune_server.event_bus import Event, EventType

        async def _on_playback_started(event: Event) -> None:
            logger.debug(
                "hello_plugin_playback",
                track=event.data.get("track_title", "?"),
                zone=event.data.get("zone_id", "?"),
            )

        self._unsub_event = ctx.event_bus.on(
            EventType.PLAYBACK_STARTED, _on_playback_started
        )

    async def teardown(self) -> None:
        """Called on graceful server shutdown."""
        if self._unsub_event:
            self._unsub_event()
        logger.info("hello_plugin_teardown")

    def config_schema(self) -> Optional[type[BaseSettings]]:
        """Return the settings class so the admin API can introspect config."""
        return HelloSettings

    # ---- Hook callbacks ----

    @staticmethod
    async def _on_track_start(zone_id: int, track) -> None:
        """Player hook: log every track start."""
        logger.info(
            "hello_plugin_track_start",
            zone_id=zone_id,
            track_title=getattr(track, "title", "?"),
            artist=getattr(track, "artist_name", "?"),
        )

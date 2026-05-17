"""Hello World plugin -- minimal example of the Tune Plugin SDK.

Demonstrates the full plugin lifecycle:
  1. Implement the ``TunePlugin`` protocol (name, version, setup, teardown)
  2. Use ``PluginContext`` to subscribe to server events
  3. Declare settings via ``PluginConfig``

Install for local testing::

    pip install -e examples/plugin_hello

Then restart the server. The plugin logs a greeting on startup and
listens for playback events.
"""
from __future__ import annotations

from tune_server.plugin_sdk import PluginConfig, PluginContext, TunePlugin


class HelloConfig(PluginConfig):
    """Settings for the Hello plugin."""

    greeting: str = "Hello from plugin!"


class HelloPlugin:
    """Minimal plugin satisfying the TunePlugin protocol."""

    name: str = "hello"
    version: str = "0.1.0"
    description: str = "Example plugin -- logs a greeting on startup"

    def __init__(self) -> None:
        self._unsubscribe: object | None = None

    async def setup(self, ctx: PluginContext) -> None:
        config = HelloConfig()
        ctx.logger.info("hello_plugin_setup: %s", config.greeting)

        # Subscribe to playback events as a demonstration
        from tune_server.event_bus import EventType

        async def _on_playback(event) -> None:
            ctx.logger.debug(
                "hello_plugin_playback: %s",
                event.data.get("track_title", "?"),
            )

        self._unsubscribe = ctx.event_bus.on(
            EventType.PLAYBACK_STARTED, _on_playback
        )

    async def teardown(self) -> None:
        if self._unsubscribe and callable(self._unsubscribe):
            self._unsubscribe()

    def config_schema(self) -> dict:
        return HelloConfig.model_json_schema()

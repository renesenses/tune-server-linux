"""Plugin discovery + lifecycle orchestration.

Reads ``entry_points("tune_server.plugins")`` to find installed plugins,
instantiates each one, and drives them through setup() / teardown().
Failures are logged and isolated — one broken plugin must not prevent
the server from booting or other plugins from loading.
"""
from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Callable

import structlog

from tune_server.plugins.api import PROTOCOL_VERSION, PluginContext, TunePlugin

logger = structlog.get_logger()

ENTRY_POINT_GROUP = "tune_server.plugins"


class PluginLoader:
    """Discovers, instantiates, and tears down plugins."""

    def __init__(self) -> None:
        self._plugins: list[TunePlugin] = []
        # Hooks registered before any zone exists need to be applied to
        # every Player created by ZoneManager. We store them here so the
        # ZoneManager can ask us at zone creation time.
        self._pending_player_hooks: list[tuple[Any, Callable]] = []

    @property
    def plugins(self) -> list[TunePlugin]:
        return list(self._plugins)

    @property
    def pending_player_hooks(self) -> list[tuple[Any, Callable]]:
        """Hooks plugins have requested. Apply each to every new Player."""
        return list(self._pending_player_hooks)

    async def discover_and_setup(self, ctx: PluginContext) -> None:
        """Find all installed plugins via entry_points and call setup() on each.

        Errors are isolated per-plugin: a broken plugin is skipped, the rest load.
        """
        # Wire the context's pending-hook registry to ours so register_player_hook
        # populates our list rather than a per-context throwaway.
        ctx._player_hook_registry = self._pending_player_hooks

        eps = entry_points(group=ENTRY_POINT_GROUP)
        if not eps:
            logger.info("no_plugins_found", group=ENTRY_POINT_GROUP)
            return

        for ep in eps:
            try:
                plugin_cls = ep.load()
                plugin: TunePlugin = plugin_cls()
            except Exception:
                logger.exception("plugin_load_failed", entry_point=ep.name)
                continue

            if not isinstance(plugin, TunePlugin):
                logger.warning(
                    "plugin_protocol_mismatch",
                    entry_point=ep.name,
                    has_attrs=[a for a in ("name", "version", "setup", "teardown") if hasattr(plugin, a)],
                )
                continue

            try:
                await plugin.setup(ctx)
            except Exception:
                logger.exception(
                    "plugin_setup_failed",
                    plugin_name=getattr(plugin, "name", ep.name),
                )
                continue

            self._plugins.append(plugin)
            logger.info(
                "plugin_loaded",
                plugin_name=plugin.name,
                version=plugin.version,
                protocol_version=PROTOCOL_VERSION,
            )

    async def teardown_all(self) -> None:
        """Tear down every loaded plugin in reverse setup order."""
        for plugin in reversed(self._plugins):
            try:
                await plugin.teardown()
            except Exception:
                logger.exception(
                    "plugin_teardown_failed",
                    plugin_name=getattr(plugin, "name", "?"),
                )
        self._plugins.clear()

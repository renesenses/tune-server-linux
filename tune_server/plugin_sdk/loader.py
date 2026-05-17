"""PluginLoader -- discovers and orchestrates SDK plugins.

Reads ``entry_points("tune.plugins")`` to find installed third-party
packages, instantiates each plugin class, creates a ``PluginContext``,
and drives the setup/teardown lifecycle.

Failures are isolated per-plugin: one broken plugin is logged and
skipped, the rest continue loading normally.
"""
from __future__ import annotations

import logging
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import aiohttp
import structlog

from tune_server.plugin_sdk.context import PluginContext
from tune_server.plugin_sdk.protocol import TunePlugin

logger = structlog.get_logger()

ENTRY_POINT_GROUP = "tune.plugins"


class PluginLoader:
    """Discovers, instantiates, and tears down SDK plugins."""

    def __init__(self, *, data_root: Path | None = None) -> None:
        self._plugins: list[TunePlugin] = []
        self._http_session: aiohttp.ClientSession | None = None
        self._data_root = data_root or Path.home() / ".tune" / "plugins"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def discover_and_setup(
        self,
        *,
        api_base_url: str,
        event_bus: Any,
    ) -> None:
        """Find all installed SDK plugins and call ``setup()`` on each.

        Parameters
        ----------
        api_base_url:
            Root URL of the Tune REST API (e.g. ``http://0.0.0.0:8888``).
        event_bus:
            The running ``EventBus`` instance.
        """
        eps = entry_points(group=ENTRY_POINT_GROUP)
        if not eps:
            logger.info("plugin_sdk_no_plugins_found", group=ENTRY_POINT_GROUP)
            return

        self._http_session = aiohttp.ClientSession()

        for ep in eps:
            # -- Load class -------------------------------------------------
            try:
                plugin_cls = ep.load()
                plugin: TunePlugin = plugin_cls()
            except Exception as exc:
                logger.exception(
                    "plugin_sdk_load_failed",
                    entry_point=ep.name,
                    error=str(exc),
                )
                continue

            if not isinstance(plugin, TunePlugin):
                logger.warning(
                    "plugin_sdk_protocol_mismatch",
                    entry_point=ep.name,
                    has_attrs=[
                        a
                        for a in ("name", "version", "setup", "teardown")
                        if hasattr(plugin, a)
                    ],
                )
                continue

            # -- Build per-plugin context -----------------------------------
            plugin_name = getattr(plugin, "name", ep.name)
            data_dir = self._data_root / plugin_name
            data_dir.mkdir(parents=True, exist_ok=True)

            plugin_logger = logging.getLogger(f"tune.plugins.{plugin_name}")

            ctx = PluginContext(
                api_base_url=api_base_url,
                data_dir=data_dir,
                logger=plugin_logger,
                event_bus=event_bus,
                http_session=self._http_session,
            )

            # -- Setup -------------------------------------------------------
            try:
                await plugin.setup(ctx)
            except Exception:
                logger.exception(
                    "plugin_sdk_setup_failed",
                    plugin_name=plugin_name,
                )
                continue

            self._plugins.append(plugin)
            logger.info(
                "plugin_sdk_loaded",
                plugin_name=plugin.name,
                version=plugin.version,
            )

    async def teardown_all(self) -> None:
        """Tear down every loaded plugin in reverse setup order."""
        for plugin in reversed(self._plugins):
            try:
                await plugin.teardown()
            except Exception:
                logger.exception(
                    "plugin_sdk_teardown_failed",
                    plugin_name=getattr(plugin, "name", "?"),
                )
        self._plugins.clear()

        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def loaded_plugins(self) -> list[TunePlugin]:
        """Return a copy of the list of successfully loaded plugins."""
        return list(self._plugins)

"""Plugin discovery + lifecycle orchestration.

Reads ``entry_points("tune_server.plugins")`` to find installed plugins,
instantiates each one, and drives them through setup() / teardown().
Failures are logged and isolated — one broken plugin must not prevent
the server from booting or other plugins from loading.
"""
from __future__ import annotations

import json
from importlib.metadata import entry_points
from typing import Any, Callable

import structlog

from tune_server.plugins.api import PROTOCOL_VERSION, PluginContext, TunePlugin

logger = structlog.get_logger()

ENTRY_POINT_GROUP = "tune_server.plugins"


class PluginLoader:
    """Discovers, instantiates, and tears down plugins."""

    def __init__(self) -> None:
        self._plugins_list: list[TunePlugin] = []
        # Name → loaded plugin instance (active plugins only)
        self.plugins: dict[str, TunePlugin] = {}
        # Name → error message for plugins that failed to load/setup
        self.failed: dict[str, str] = {}
        # Track what each plugin registered so the API can report it
        self._registered_output_types: dict[str, list[str]] = {}
        self._registered_routes: dict[str, list[str]] = {}
        self._registered_hooks: dict[str, int] = {}
        # Hooks registered before any zone exists need to be applied to
        # every Player created by ZoneManager. We store them here so the
        # ZoneManager can ask us at zone creation time.
        self._pending_player_hooks: list[tuple[Any, Callable]] = []

    @property
    def pending_player_hooks(self) -> list[tuple[Any, Callable]]:
        """Hooks plugins have requested. Apply each to every new Player."""
        return list(self._pending_player_hooks)

    async def _is_disabled(self, name: str, db: Any) -> bool:
        """Check if a plugin is disabled in the database."""
        if db is None:
            return False
        try:
            row = await db.fetchone(
                "SELECT token_data FROM streaming_auth WHERE service = :service",
                {"service": f"plugin_{name}"},
            )
            if row:
                data = json.loads(row["token_data"])
                return not data.get("enabled", True)
        except Exception:
            pass
        return False

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
            except Exception as exc:
                logger.exception("plugin_load_failed", entry_point=ep.name)
                self.failed[ep.name] = f"Load error: {exc}"
                continue

            if not isinstance(plugin, TunePlugin):
                msg = "Does not satisfy TunePlugin protocol"
                logger.warning(
                    "plugin_protocol_mismatch",
                    entry_point=ep.name,
                    has_attrs=[a for a in ("name", "version", "setup", "teardown") if hasattr(plugin, a)],
                )
                self.failed[ep.name] = msg
                continue

            plugin_name = getattr(plugin, "name", ep.name)

            # Check disabled state in DB
            if await self._is_disabled(plugin_name, ctx.db):
                logger.info("plugin_disabled", plugin_name=plugin_name)
                self.failed[plugin_name] = "disabled"
                continue

            # Track registrations by wrapping context methods
            hooks_before = len(self._pending_player_hooks)
            output_types_registered: list[str] = []
            routes_registered: list[str] = []

            original_register_output = ctx.register_output_type
            original_register_router = ctx.register_router
            original_register_hook = ctx.register_player_hook

            def _track_output(spec, _ot=output_types_registered, _orig=original_register_output):
                _ot.append(spec.name)
                return _orig(spec)

            def _track_router(router, prefix="/api/v1", _rt=routes_registered, _orig=original_register_router):
                for route in getattr(router, "routes", []):
                    path = getattr(route, "path", "")
                    _rt.append(f"{prefix}{path}")
                return _orig(router, prefix)

            def _track_hook(event, fn, _orig=original_register_hook):
                return _orig(event, fn)

            ctx.register_output_type = _track_output  # type: ignore[assignment]
            ctx.register_router = _track_router  # type: ignore[assignment]
            ctx.register_player_hook = _track_hook  # type: ignore[assignment]

            try:
                await plugin.setup(ctx)
            except Exception as exc:
                logger.exception(
                    "plugin_setup_failed",
                    plugin_name=plugin_name,
                )
                self.failed[plugin_name] = f"Setup error: {exc}"
                continue
            finally:
                # Restore original methods
                ctx.register_output_type = original_register_output  # type: ignore[assignment]
                ctx.register_router = original_register_router  # type: ignore[assignment]
                ctx.register_player_hook = original_register_hook  # type: ignore[assignment]

            hooks_added = len(self._pending_player_hooks) - hooks_before
            self._registered_output_types[plugin_name] = output_types_registered
            self._registered_routes[plugin_name] = routes_registered
            self._registered_hooks[plugin_name] = hooks_added

            self._plugins_list.append(plugin)
            self.plugins[plugin_name] = plugin
            logger.info(
                "plugin_loaded",
                plugin_name=plugin.name,
                version=plugin.version,
                protocol_version=PROTOCOL_VERSION,
                output_types=output_types_registered,
                routes=len(routes_registered),
                hooks=hooks_added,
            )

    def list_plugins(self) -> list[dict[str, Any]]:
        """Return structured info about all plugins (loaded + failed).

        Used by the /system/plugins API endpoint.
        """
        result: list[dict[str, Any]] = []

        for name, plugin in self.plugins.items():
            config_cls = None
            try:
                config_cls = plugin.config_schema()
            except Exception:
                pass

            result.append({
                "name": name,
                "version": getattr(plugin, "version", "?"),
                "description": getattr(plugin, "description", ""),
                "status": "active",
                "output_types": self._registered_output_types.get(name, []),
                "routes": self._registered_routes.get(name, []),
                "hooks_count": self._registered_hooks.get(name, 0),
                "has_config": config_cls is not None,
            })

        for name, error in self.failed.items():
            result.append({
                "name": name,
                "version": "?",
                "description": "",
                "status": "disabled" if error == "disabled" else "error",
                "error": error if error != "disabled" else None,
                "output_types": [],
                "routes": [],
                "hooks_count": 0,
                "has_config": False,
            })

        return result

    async def teardown_all(self) -> None:
        """Tear down every loaded plugin in reverse setup order."""
        for plugin in reversed(self._plugins_list):
            try:
                await plugin.teardown()
            except Exception:
                logger.exception(
                    "plugin_teardown_failed",
                    plugin_name=getattr(plugin, "name", "?"),
                )
        self._plugins_list.clear()
        self.plugins.clear()

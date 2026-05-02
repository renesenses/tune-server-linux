"""Plugin Protocol + descriptors + context.

This module defines the public surface plugins implement and consume.
Stable across a major PROTOCOL_VERSION; breaking changes bump it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from fastapi import APIRouter, FastAPI
from pydantic_settings import BaseSettings

from tune_server.audio.formats import AudioCapabilities
from tune_server.event_bus import EventBus
from tune_server.models import OutputType
from tune_server.playback.player import PlayerHookEvent

# Plugins must check this against the value they were tested with.
# Bump on breaking changes (signature, semantics, removed APIs).
PROTOCOL_VERSION = "1"


# Factory signature: receives the optional output_device_id (e.g. DLNA UUID,
# CoreAudio UID) and returns a configured OutputTarget instance.
OutputFactory = Callable[[Optional[str]], Awaitable[Any]]


@dataclass
class OutputTypeSpec:
    """Descriptor a plugin uses to register a new output type.

    Built-in output types (local, dlna, airplay) are registered the same way
    by the core itself. Plugins use a free-form ``name`` (no enum needed).
    """
    name: str
    """Lowercase identifier, e.g. "snapcast", "icecast", "captureservice"."""

    factory: OutputFactory
    """Async callable returning a fully configured OutputTarget."""

    capabilities: AudioCapabilities
    """Audio formats / sample rates / bit depths the output accepts."""

    description: str = ""
    """One-line human-readable description, shown in the web UI."""

    icon: Optional[str] = None
    """SF Symbol name or material-icons name for the web UI; optional."""


@dataclass
class PluginContext:
    """Passed to plugin.setup(). Provides safe access to core hooks.

    Plugins call the register_* methods to wire themselves into the running
    server. After setup() returns, the plugin's contributions are live.
    """
    event_bus: EventBus
    api_app: FastAPI
    db: Any  # Database — typed as Any to avoid circular import at module load
    settings: Any  # Settings — Any for the same reason
    _zone_manager: Any = None  # ZoneManager — populated by loader at runtime
    _player_hook_registry: list = None  # type: ignore  # populated by loader

    # ---- Registration helpers ----

    def register_output_type(self, spec: OutputTypeSpec) -> None:
        """Register a new output type. Equivalent to
        ``zone_manager.register_output_factory(spec.name, spec.factory)``
        plus the capabilities/UI metadata."""
        if self._zone_manager is None:
            raise RuntimeError("PluginContext not fully initialised")
        self._zone_manager.register_output_factory(spec.name, spec.factory)
        # Capabilities + description are stored on the context so the API
        # can expose them later via /api/v1/system/output-types.
        if not hasattr(self._zone_manager, "_output_specs"):
            self._zone_manager._output_specs = {}
        self._zone_manager._output_specs[spec.name] = spec

    def register_router(self, router: APIRouter, prefix: str = "/api/v1") -> None:
        """Mount a FastAPI router contributed by the plugin."""
        self.api_app.include_router(router, prefix=prefix)

    def register_player_hook(
        self, event: PlayerHookEvent, fn: Callable
    ) -> None:
        """Register a callable for a player lifecycle event.

        Hook is applied to ALL players (existing + future). Idempotent —
        a hook registered before any zone exists will still fire on the
        first track of the first zone.
        """
        if self._player_hook_registry is None:
            raise RuntimeError("PluginContext not fully initialised")
        self._player_hook_registry.append((event, fn))

    def register_migrations(self, path: Path) -> None:
        """Declare a directory of alembic migrations the plugin owns.

        Migrations are NOT run automatically at boot. They run on
        explicit ``tune-server migrate --plugin <name>`` invocation.
        Plugin tables MUST be prefixed with ``<plugin_name>_*``.
        """
        # V1: store on context. Real alembic wiring deferred to PoC 4+.
        if not hasattr(self, "_migrations"):
            self._migrations = []
        self._migrations.append(path)


@runtime_checkable
class TunePlugin(Protocol):
    """Plugin interface. Any class with these attrs/methods qualifies.

    Lifecycle:
        1. Loader instantiates the class (no-arg ``__init__``).
        2. Loader awaits ``setup(ctx)`` once, after core init, before zones.
        3. Plugin lives until shutdown.
        4. Loader awaits ``teardown()`` on graceful shutdown.

    Plugins should NOT do work in ``__init__``. Defer to ``setup()`` so
    failures are caught and logged uniformly.
    """
    name: str
    version: str

    async def setup(self, ctx: PluginContext) -> None: ...
    async def teardown(self) -> None: ...

    def config_schema(self) -> Optional[type[BaseSettings]]:
        """Return a pydantic-settings class with prefix
        ``TUNE_<NAME>_*``. None = the plugin has no settings."""
        ...

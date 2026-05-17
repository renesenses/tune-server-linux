"""TunePlugin Protocol -- the interface every plugin must satisfy.

A plugin is any class whose public attributes and methods match this
Protocol. Duck typing via ``runtime_checkable`` means there is no
mandatory base class -- plain classes work out of the box.

Lifecycle:
    1. Loader instantiates the class (no-arg ``__init__``).
    2. Loader awaits ``setup(ctx)`` once, after core init, before zones.
    3. Plugin lives until server shutdown.
    4. Loader awaits ``teardown()`` on graceful shutdown.

Plugins should NOT do heavy work in ``__init__``. Defer to ``setup()``
so failures are caught and logged uniformly by the loader.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel

from tune_server.plugin_sdk.context import PluginContext


@runtime_checkable
class TunePlugin(Protocol):
    """Plugin interface.  Any class with these attrs/methods qualifies."""

    # ---- Identity (class-level or instance attributes) ----

    @property
    def name(self) -> str:
        """Unique short identifier, e.g. ``"scrobbler"``, ``"snapcast"``."""
        ...

    @property
    def version(self) -> str:
        """SemVer string, e.g. ``"0.2.1"``."""
        ...

    @property
    def description(self) -> str:
        """One-line human-readable summary shown in the admin UI."""
        ...

    # ---- Lifecycle ----

    async def setup(self, ctx: PluginContext) -> None:
        """Called once after core init.  Register routes, hooks, listeners."""
        ...

    async def teardown(self) -> None:
        """Called on graceful shutdown.  Release resources, unsubscribe."""
        ...

    # ---- Configuration ----

    def config_schema(self) -> dict:
        """Return a JSON Schema ``dict`` describing the plugin's settings.

        The schema is used by the admin UI to render a config form and by
        the loader to validate user-supplied values.

        Return an empty ``dict`` if the plugin has no configurable settings.
        """
        ...

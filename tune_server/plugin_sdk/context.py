"""PluginContext -- runtime services injected into every plugin at setup().

The context is a read-only snapshot of the running server's capabilities.
Plugins use it to subscribe to events, issue HTTP requests, and persist
data in their own directory.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiohttp

    from tune_server.event_bus import EventBus


@dataclass(frozen=True)
class PluginContext:
    """Injected into ``TunePlugin.setup()``.

    Attributes are intentionally minimal -- only what a third-party
    plugin genuinely needs. Internal wiring (DB handle, zone manager,
    FastAPI app) stays in the private ``tune_server.plugins`` package.

    Parameters
    ----------
    api_base_url:
        Root URL of the running Tune REST API,
        e.g. ``"http://192.168.1.18:8888"``.
    data_dir:
        Writable directory private to this plugin.  Created by the
        loader before ``setup()`` is called.
    logger:
        Pre-configured ``logging.Logger`` namespaced to the plugin,
        e.g. ``tune.plugins.scrobbler``.
    event_bus:
        The server's async pub/sub bus.  Call ``event_bus.on(...)`` to
        subscribe to library, playback, zone, or system events.
    http_session:
        Shared ``aiohttp.ClientSession`` for outbound HTTP calls.
        Plugins must NOT close this session -- the loader manages its
        lifecycle.
    """

    api_base_url: str
    data_dir: Path
    logger: logging.Logger
    event_bus: EventBus
    http_session: aiohttp.ClientSession

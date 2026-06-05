"""Public type re-exports for plugin developers.

Import from this module to get all the types needed to build a Tune plugin::

    from tune_server.plugin_sdk.types import (
        TunePlugin,
        PluginContext,
        OutputTypeSpec,
        OutputFactory,
        PROTOCOL_VERSION,
        PlayerHookEvent,
        EventBus,
        Event,
        EventType,
        AudioCapabilities,
        AudioFormat,
        OutputTarget,
    )

This module is a convenience facade. All types originate from their
respective modules in the core server — see each class's docstring for
full documentation.
"""
from __future__ import annotations

# -- Plugin Protocol & Context (core plugin API) --

from tune_server.plugins.api import (
    PROTOCOL_VERSION,
    OutputFactory,
    OutputTypeSpec,
    PluginContext,
    TunePlugin,
)

# -- Event Bus --

from tune_server.event_bus import (
    Event,
    EventBus,
    EventType,
)

# -- Player Hooks --

from tune_server.playback.player import PlayerHookEvent

# -- Audio --

from tune_server.audio.formats import AudioCapabilities
from tune_server.models import AudioFormat
from tune_server.outputs.base import OutputTarget

__all__ = [
    # Plugin API
    "PROTOCOL_VERSION",
    "TunePlugin",
    "PluginContext",
    "OutputTypeSpec",
    "OutputFactory",
    # Events
    "EventBus",
    "Event",
    "EventType",
    # Player hooks
    "PlayerHookEvent",
    # Audio
    "AudioCapabilities",
    "AudioFormat",
    "OutputTarget",
]

"""Tune Server plugin system.

Public SDK for third-party plugins. Plugins extend Tune Server with new
output types, event handlers, REST routes, player hooks, and config.

Plugin discovery uses Python entry points::

    [project.entry-points."tune_server.plugins"]
    myplugin = "my_package:MyPlugin"

A plugin is any class that satisfies the TunePlugin Protocol (see
``tune_server.plugins.api``). The PluginLoader instantiates each plugin
once at server startup and calls ``setup(ctx)`` with a PluginContext
giving safe access to core hooks.

Backward / forward compatibility is tracked via ``PROTOCOL_VERSION``.
Bumping it is a breaking change — plugins must check it in their
``setup()``.
"""
from tune_server.plugins.api import (
    PROTOCOL_VERSION,
    OutputTypeSpec,
    PluginContext,
    TunePlugin,
)
from tune_server.plugins.loader import PluginLoader

__all__ = [
    "PROTOCOL_VERSION",
    "OutputTypeSpec",
    "PluginContext",
    "PluginLoader",
    "TunePlugin",
]

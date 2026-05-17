"""Tune Plugin SDK -- public interface for third-party plugins.

Install ``tune-server`` and import from this package to build a plugin::

    from tune_server.plugin_sdk import TunePlugin, PluginContext, PluginConfig

See ``examples/plugin_hello/`` for a minimal working plugin.
"""
from tune_server.plugin_sdk.config import PluginConfig
from tune_server.plugin_sdk.context import PluginContext
from tune_server.plugin_sdk.protocol import TunePlugin

__all__ = [
    "PluginConfig",
    "PluginContext",
    "TunePlugin",
]

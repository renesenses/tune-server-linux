"""PluginConfig -- base model for plugin settings.

Third-party plugins subclass ``PluginConfig`` to declare their own
settings. Values are loaded from environment variables prefixed with
``TUNE_<PLUGIN_NAME>_`` (handled by pydantic-settings).

Example::

    class MyPluginConfig(PluginConfig):
        model_config = SettingsConfigDict(env_prefix="TUNE_MYPLUGIN_")

        api_key: str = ""
        max_retries: int = 3
"""
from __future__ import annotations

from pydantic import BaseModel


class PluginConfig(BaseModel):
    """Base configuration model for Tune plugins.

    Subclass this and add your own fields. Call
    ``model_json_schema()`` to produce the JSON Schema dict that
    ``TunePlugin.config_schema()`` should return.
    """

    class Config:
        extra = "ignore"

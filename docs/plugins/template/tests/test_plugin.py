"""Tests for the example plugin."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from tune_server.event_bus import EventBus
from tune_server.plugins.api import PluginContext

from my_tune_plugin import ExamplePlugin


@pytest.fixture
def plugin_context():
    """Create a minimal PluginContext for testing."""
    return PluginContext(
        event_bus=EventBus(),
        api_app=MagicMock(),
        db=AsyncMock(),
        settings=MagicMock(),
        _zone_manager=MagicMock(),
        _player_hook_registry=[],
    )


@pytest.mark.asyncio
async def test_setup(plugin_context):
    plugin = ExamplePlugin()
    await plugin.setup(plugin_context)
    assert plugin.name == "example"
    assert plugin.version == "0.1.0"


@pytest.mark.asyncio
async def test_teardown(plugin_context):
    plugin = ExamplePlugin()
    await plugin.setup(plugin_context)
    await plugin.teardown()  # should not raise


@pytest.mark.asyncio
async def test_config_schema():
    plugin = ExamplePlugin()
    schema = plugin.config_schema()
    assert schema is not None
    # Verify it's a pydantic-settings class
    instance = schema()
    assert hasattr(instance, "enabled")
    assert hasattr(instance, "greeting")

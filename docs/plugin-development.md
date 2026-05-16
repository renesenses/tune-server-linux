# Tune Server Plugin Development Guide

Build plugins that extend Tune Server with new output types, API endpoints, event handlers, and player hooks.

## Quick Start (5 minutes)

### 1. Create a Python package

```
my-tune-plugin/
  pyproject.toml
  my_tune_plugin/
    __init__.py
```

### 2. Write the plugin class

```python
# my_tune_plugin/__init__.py
from __future__ import annotations
from typing import Optional

import structlog
from fastapi import APIRouter
from pydantic_settings import BaseSettings

from tune_server.plugins.api import PluginContext, TunePlugin
from tune_server.playback.player import PlayerHookEvent

logger = structlog.get_logger()

router = APIRouter(tags=["my-plugin"])

@router.get("/my-endpoint")
async def my_endpoint():
    return {"status": "ok"}


class MyPlugin:
    name = "my_plugin"
    version = "0.1.0"
    description = "My first Tune plugin"

    async def setup(self, ctx: PluginContext) -> None:
        ctx.register_router(router)
        logger.info("my_plugin_loaded")

    async def teardown(self) -> None:
        logger.info("my_plugin_stopped")

    def config_schema(self) -> Optional[type[BaseSettings]]:
        return None
```

### 3. Declare the entry point

```toml
# pyproject.toml
[project]
name = "my-tune-plugin"
version = "0.1.0"
dependencies = ["tune-server"]

[project.entry-points."tune_server.plugins"]
my_plugin = "my_tune_plugin:MyPlugin"
```

### 4. Install and run

```bash
pip install -e ./my-tune-plugin
tune-server  # plugin auto-discovered at startup
```

Your endpoint is live at `GET /api/v1/my-endpoint`.

---

## Plugin Lifecycle

```
__init__()  →  setup(ctx)  →  [live]  →  teardown()
```

1. **`__init__()`** — The loader instantiates your class with no arguments. Do NOT do any work here. No I/O, no imports of heavy libs. Defer everything to `setup()`.

2. **`setup(ctx: PluginContext)`** — Called once after core init (database, event bus, zone manager) but before zones spawn. This is where you register routes, hooks, event listeners, and output types. The `ctx` object provides safe access to core services.

3. **Live** — Your routes handle requests, your hooks fire on player events, your event listeners receive bus events. The plugin stays active until the server shuts down.

4. **`teardown()`** — Called on graceful shutdown (SIGTERM, `/system/restart`). Unsubscribe from events, close connections, flush buffers.

### Error isolation

If your plugin raises during `setup()`, it is skipped. Other plugins still load. The error is logged and reported via `GET /api/v1/system/plugins`.

---

## TunePlugin Protocol

Your class must satisfy this protocol (duck typing, no inheritance needed):

```python
class TunePlugin(Protocol):
    name: str                           # Unique lowercase identifier
    version: str                        # Semver string
    async def setup(self, ctx: PluginContext) -> None: ...
    async def teardown(self) -> None: ...
    def config_schema(self) -> Optional[type[BaseSettings]]: ...
```

Optional attribute:
- `description: str` — One-line summary shown in the plugin list API.

---

## PluginContext API

The `ctx` object passed to `setup()` exposes these services and registration methods.

### Available services

| Attribute     | Type        | Description                                    |
|---------------|-------------|------------------------------------------------|
| `event_bus`   | `EventBus`  | Async pub/sub — subscribe to or emit events    |
| `api_app`     | `FastAPI`   | The FastAPI application instance               |
| `db`          | `Database`  | Async database (SQLite or PostgreSQL)           |
| `settings`    | `Settings`  | Server configuration (read-only recommended)   |

### `ctx.register_output_type(spec: OutputTypeSpec)`

Register a new audio output type (e.g., Snapcast, Icecast).

```python
from tune_server.plugins.api import OutputTypeSpec
from tune_server.audio.formats import AudioCapabilities

spec = OutputTypeSpec(
    name="snapcast",
    factory=my_output_factory,
    capabilities=AudioCapabilities(
        formats=["pcm"],
        sample_rates=[44100, 48000, 96000],
        bit_depths=[16, 24],
    ),
    description="Snapcast multi-room output",
    icon="speaker.wave.3",
)
ctx.register_output_type(spec)
```

The `factory` is an async callable `(Optional[str]) -> OutputTarget` that receives the output device ID and returns a configured output target.

### `ctx.register_router(router: APIRouter, prefix: str = "/api/v1")`

Mount a FastAPI router. All routes are prefixed.

```python
from fastapi import APIRouter

router = APIRouter(tags=["my-plugin"])

@router.get("/my-status")
async def status():
    return {"ok": True}

# In setup():
ctx.register_router(router)
# Route available at GET /api/v1/my-status
```

### `ctx.register_player_hook(event: PlayerHookEvent, fn: Callable)`

Register a callback for player lifecycle events. Hooks fire for ALL zones.

```python
from tune_server.playback.player import PlayerHookEvent

async def on_track_start(zone_id: int, track) -> None:
    logger.info("track_started", title=track.title)

ctx.register_player_hook(PlayerHookEvent.BEFORE_TRACK, on_track_start)
```

Available hook events:

| Event           | Arguments              | When                                |
|-----------------|------------------------|-------------------------------------|
| `BEFORE_TRACK`  | `(zone_id, track)`     | Just before audio output starts     |
| `AFTER_TRACK`   | `(zone_id, track)`     | When a track ends naturally         |
| `PLAY`          | `(zone_id,)`           | On play/resume                      |
| `PAUSE`         | `(zone_id,)`           | On pause                            |
| `STOP`          | `(zone_id,)`           | On stop                             |

Both sync and async callables are supported.

### `ctx.register_migrations(path: Path)`

Declare a directory of Alembic migrations your plugin owns. Migrations run on explicit `tune-server migrate --plugin <name>` invocation. Plugin tables MUST be prefixed with `<plugin_name>_*`.

```python
from pathlib import Path
ctx.register_migrations(Path(__file__).parent / "migrations")
```

---

## EventBus

Subscribe to built-in events or emit your own custom events.

### Subscribing to built-in events

```python
from tune_server.event_bus import Event, EventType

async def on_scan_complete(event: Event) -> None:
    logger.info("scan_done", tracks=event.data.get("tracks_added", 0))

# Returns an unsubscribe callable
unsub = ctx.event_bus.on(EventType.LIBRARY_SCAN_COMPLETED, on_scan_complete)

# To unsubscribe (e.g., in teardown):
unsub()
```

### Custom string events

Plugins can emit and subscribe to custom string event types (no enum needed):

```python
from tune_server.event_bus import Event

# Subscribe
unsub = ctx.event_bus.on("snapcast.client_connected", my_handler)

# Emit
await ctx.event_bus.emit(Event(
    type="snapcast.client_connected",
    data={"client_id": "abc123", "name": "Kitchen"},
    source="snapcast_plugin",
))
```

Convention: use `<plugin_name>.<event>` for custom events.

### Subscribe to all events

```python
unsub = ctx.event_bus.on_all(my_handler)
```

---

## Configuration

Use `pydantic-settings` with prefix `TUNE_<NAME>_*`:

```python
from pydantic_settings import BaseSettings

class MyPluginSettings(BaseSettings):
    host: str = "localhost"
    port: int = 1780
    buffer_ms: int = 500

    model_config = {"env_prefix": "TUNE_MYPLUGIN_"}
```

Users configure via environment variables or `.env`:

```bash
TUNE_MYPLUGIN_HOST=192.168.1.50
TUNE_MYPLUGIN_PORT=1780
```

Return the class from `config_schema()` so the admin API can introspect it:

```python
def config_schema(self):
    return MyPluginSettings
```

---

## Plugin Management API

Tune Server exposes these endpoints for managing plugins at runtime:

| Method | Endpoint                            | Description                         |
|--------|-------------------------------------|-------------------------------------|
| GET    | `/api/v1/system/plugins`            | List all plugins with status        |
| GET    | `/api/v1/system/plugins/{name}`     | Detailed info for one plugin        |
| POST   | `/api/v1/system/plugins/{name}/disable` | Disable (skipped on next boot) |
| POST   | `/api/v1/system/plugins/{name}/enable`  | Re-enable                      |

Example response from `GET /api/v1/system/plugins`:

```json
[
  {
    "name": "hello",
    "version": "0.1.0",
    "description": "Example plugin",
    "status": "active",
    "output_types": [],
    "routes": ["/api/v1/hello"],
    "hooks_count": 1,
    "has_config": true
  }
]
```

---

## Packaging for PyPI

Full `pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "tune-plugin-myplugin"
version = "0.1.0"
description = "My Tune Server plugin"
requires-python = ">=3.11"
dependencies = [
    "tune-server>=0.7.0",
]

[project.entry-points."tune_server.plugins"]
myplugin = "tune_plugin_myplugin:MyPlugin"
```

### Naming convention

- Package name: `tune-plugin-<name>` (on PyPI)
- Entry point key: `<name>` (the plugin's `name` attribute)
- Env prefix: `TUNE_<NAME>_*`
- DB table prefix: `<name>_*`

---

## Testing Your Plugin

```python
# tests/test_my_plugin.py
import pytest
from unittest.mock import AsyncMock, MagicMock

from tune_server.plugins.api import PluginContext
from tune_server.event_bus import EventBus

from my_tune_plugin import MyPlugin


@pytest.fixture
def plugin_context():
    """Create a minimal PluginContext for testing."""
    ctx = PluginContext(
        event_bus=EventBus(),
        api_app=MagicMock(),
        db=AsyncMock(),
        settings=MagicMock(),
        _zone_manager=MagicMock(),
        _player_hook_registry=[],
    )
    return ctx


@pytest.mark.asyncio
async def test_plugin_setup(plugin_context):
    plugin = MyPlugin()
    await plugin.setup(plugin_context)
    assert plugin.name == "my_plugin"


@pytest.mark.asyncio
async def test_plugin_teardown(plugin_context):
    plugin = MyPlugin()
    await plugin.setup(plugin_context)
    await plugin.teardown()  # should not raise
```

Run tests:

```bash
pip install pytest pytest-asyncio
pytest tests/
```

---

## Protocol Version

The SDK declares `PROTOCOL_VERSION = "1"`. If Tune Server makes breaking changes to the plugin API (signatures, removed methods), this version is bumped. Your plugin can check it in `setup()`:

```python
from tune_server.plugins.api import PROTOCOL_VERSION

async def setup(self, ctx):
    if PROTOCOL_VERSION != "1":
        raise RuntimeError(f"Incompatible protocol: {PROTOCOL_VERSION}")
    # ... rest of setup
```

---

## Best Practices

- **No work in `__init__()`** — defer to `setup()`.
- **Use structlog** — consistent with the rest of the server.
- **Prefix DB tables** — `<plugin_name>_*` to avoid collisions.
- **Prefix env vars** — `TUNE_<NAME>_*`.
- **Unsubscribe in teardown** — avoid dangling event listeners.
- **Handle errors gracefully** — do not let your hook crash the player.
- **Log at appropriate levels** — `debug` for noisy, `info` for lifecycle, `error` for failures.

---

## Complete Example

See `tune_server/plugins/example_plugin.py` for a fully working plugin that registers an API endpoint, a player hook, an event listener, and a config schema.

Install it locally for testing:

```toml
# In your tune-server pyproject.toml [project.entry-points."tune_server.plugins"]
hello = "tune_server.plugins.example_plugin:HelloPlugin"
```

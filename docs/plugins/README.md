# Guide de Developpement de Plugins Tune / Plugin Developer Guide

> **PROTOCOL_VERSION = "1"** — Ce guide couvre la version 1 du protocole plugin.
> This guide covers plugin protocol version 1.

---

## Qu'est-ce qu'un plugin Tune ? / What Is a Tune Plugin?

Un plugin Tune est un package Python qui etend le serveur via des hooks standardises :
nouveaux types de sortie audio, routes API, ecouteurs d'evenements, hooks de lecture, widgets UI.

A Tune plugin is a Python package that extends the server through standardized hooks:
new audio output types, API routes, event listeners, player hooks, UI widgets.

Le serveur decouvre les plugins automatiquement via `entry_points("tune_server.plugins")` au
demarrage. Aucune modification du code source du serveur n'est necessaire.

**52 plugins officiels** existent deja (lyrics, scrobbling, Snapcast, Home Assistant, EQ, etc.).

---

## Quick Start (5 minutes)

### 1. Creer le package / Create the package

```
my-tune-plugin/
  pyproject.toml
  my_plugin.py
```

### 2. Ecrire la classe plugin / Write the plugin class

```python
# my_plugin.py
from __future__ import annotations
from typing import Optional

import structlog
from fastapi import APIRouter
from pydantic_settings import BaseSettings

from tune_server.plugins.api import PluginContext

logger = structlog.get_logger()

router = APIRouter(tags=["hello"])

@router.get("/hello")
async def hello():
    return {"message": "Hello from my plugin!"}


class HelloPlugin:
    name = "hello"
    version = "0.1.0"
    description = "Mon premier plugin Tune"

    async def setup(self, ctx: PluginContext) -> None:
        ctx.register_router(router)
        logger.info("hello_plugin_ready")

    async def teardown(self) -> None:
        pass

    def config_schema(self) -> Optional[type[BaseSettings]]:
        return None
```

### 3. Declarer l'entry point / Declare the entry point

```toml
# pyproject.toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "tune-plugin-hello"
version = "0.1.0"
dependencies = ["tune-server>=0.7.90"]

[project.entry-points."tune_server.plugins"]
hello = "my_plugin:HelloPlugin"
```

### 4. Installer et lancer / Install and run

```bash
pip install -e ./my-tune-plugin
tune-server  # plugin auto-discovered at startup
```

Votre endpoint est accessible a `GET /api/v1/hello`.

---

## Cycle de vie du plugin / Plugin Lifecycle

```
__init__()  -->  setup(ctx)  -->  [actif / live]  -->  teardown()
```

| Phase | Description |
|-------|-------------|
| `__init__()` | Instanciation sans arguments. **Ne rien faire ici.** Pas d'I/O, pas d'imports lourds. |
| `setup(ctx)` | Appele apres l'init du coeur (DB, event bus, zone manager). Enregistrer routes, hooks, listeners. |
| **Actif** | Les routes repondent, les hooks se declenchent, les listeners recoivent. |
| `teardown()` | Arret gracieux (SIGTERM, `/system/restart`). Liberer les ressources. |

### Isolation des erreurs / Error Isolation

Si `setup()` leve une exception, le plugin est ignore. Les autres plugins continuent de charger.
L'erreur est logguee et visible via `GET /api/v1/system/plugins`.

---

## Protocole TunePlugin / TunePlugin Protocol

Votre classe doit satisfaire ce protocole (duck typing, pas d'heritage requis) :

```python
class TunePlugin(Protocol):
    name: str          # Identifiant unique en minuscules
    version: str       # Semver, ex: "0.1.0"

    async def setup(self, ctx: PluginContext) -> None: ...
    async def teardown(self) -> None: ...
    def config_schema(self) -> Optional[type[BaseSettings]]: ...
```

Attribut optionnel :
- `description: str` — Resume affiche dans l'API `/system/plugins`.

---

## API du PluginContext / PluginContext API

L'objet `ctx` passe a `setup()` donne acces aux services du serveur.

### Services disponibles / Available Services

| Attribut | Type | Description |
|----------|------|-------------|
| `event_bus` | `EventBus` | Pub/sub async — s'abonner ou emettre des evenements |
| `api_app` | `FastAPI` | Instance de l'application FastAPI |
| `db` | `Database` | Base de donnees async (SQLite ou PostgreSQL) |
| `settings` | `Settings` | Configuration serveur (lecture seule recommandee) |

### ctx.register_output_type(spec)

Enregistrer un nouveau type de sortie audio (ex: Snapcast, Icecast).

```python
from tune_server.plugins.api import OutputTypeSpec
from tune_server.audio.formats import AudioCapabilities
from tune_server.models import AudioFormat

spec = OutputTypeSpec(
    name="snapcast",
    factory=my_output_factory,  # async (Optional[str]) -> OutputTarget
    capabilities=AudioCapabilities(
        formats={AudioFormat.FLAC, AudioFormat.WAV},
        max_sample_rate=96000,
        max_bit_depth=24,
        supports_gapless=True,
    ),
    description="Snapcast multi-room output",
    icon="speaker.wave.3",
)
ctx.register_output_type(spec)
```

### ctx.register_router(router, prefix="/api/v1")

Monter un routeur FastAPI. Toutes les routes sont prefixees.

```python
from fastapi import APIRouter

router = APIRouter(tags=["my-plugin"])

@router.get("/my-status")
async def status():
    return {"ok": True}

ctx.register_router(router)
# -> GET /api/v1/my-status
```

### ctx.register_player_hook(event, fn)

Enregistrer un callback sur le cycle de vie du lecteur. Les hooks se declenchent pour TOUTES les zones.

```python
from tune_server.playback.player import PlayerHookEvent

async def on_track_start(zone_id: int, track) -> None:
    logger.info("track_started", title=track.title)

ctx.register_player_hook(PlayerHookEvent.BEFORE_TRACK, on_track_start)
```

| Event | Arguments | Quand / When |
|-------|-----------|--------------|
| `BEFORE_TRACK` | `(zone_id, track)` | Juste avant le demarrage audio |
| `AFTER_TRACK` | `(zone_id, track)` | Fin naturelle d'une piste |
| `PLAY` | `(zone_id,)` | Play / resume |
| `PAUSE` | `(zone_id,)` | Pause |
| `STOP` | `(zone_id,)` | Stop |

Callables sync et async supportes.

### ctx.register_migrations(path)

Declarer un repertoire de migrations Alembic. Les tables du plugin DOIVENT etre prefixees `<plugin_name>_*`.

```python
from pathlib import Path
ctx.register_migrations(Path(__file__).parent / "migrations")
```

Les migrations ne sont PAS executees au boot. Elles s'executent via :
```bash
tune-server migrate --plugin <name>
```

---

## EventBus

### S'abonner aux evenements builtin / Subscribe to Built-in Events

```python
from tune_server.event_bus import Event, EventType

async def on_scan_complete(event: Event) -> None:
    logger.info("scan_done", tracks=event.data.get("tracks_added", 0))

unsub = ctx.event_bus.on(EventType.LIBRARY_SCAN_COMPLETED, on_scan_complete)
```

`unsub()` pour se desabonner (appeler dans `teardown()`).

### Types d'evenements disponibles / Available Event Types

**Library** : `LIBRARY_SCAN_STARTED`, `LIBRARY_SCAN_PROGRESS`, `LIBRARY_SCAN_COMPLETED`,
`LIBRARY_TRACK_ADDED`, `LIBRARY_TRACK_UPDATED`, `LIBRARY_TRACK_REMOVED`,
`LIBRARY_ARTWORK_PROGRESS`, `LIBRARY_ARTWORK_COMPLETED`,
`LIBRARY_ENRICH_PROGRESS`, `LIBRARY_ENRICH_COMPLETED`

**Playback** : `PLAYBACK_STARTED`, `PLAYBACK_PAUSED`, `PLAYBACK_RESUMED`, `PLAYBACK_STOPPED`,
`PLAYBACK_TRACK_CHANGED`, `PLAYBACK_POSITION`, `PLAYBACK_ERROR`,
`PLAYBACK_METADATA`, `PLAYBACK_QUEUE_CHANGED`

**Playlist** : `PLAYLIST_CREATED`, `PLAYLIST_UPDATED`, `PLAYLIST_DELETED`, `PLAYLIST_TRACKS_CHANGED`

**Zone** : `ZONE_CREATED`, `ZONE_DELETED`, `ZONE_UPDATED`, `ZONE_GROUPED`,
`ZONE_UNGROUPED`, `ZONE_VOLUME_CHANGED`

**Device** : `DEVICE_DISCOVERED`, `DEVICE_LOST`, `DEVICE_UPDATED`

**System** : `SYSTEM_STARTED`, `SYSTEM_STOPPING`, `SYSTEM_UPDATE_AVAILABLE`

**Network** : `NETWORK_SHARE_DISCOVERED`, `NETWORK_MOUNT_ADDED`, etc.

### Evenements custom / Custom Events

Les plugins peuvent emettre et ecouter des evenements string (pas besoin d'enum) :

```python
from tune_server.event_bus import Event

# Emettre / Emit
await ctx.event_bus.emit(Event(
    type="myplugin.something_happened",
    data={"key": "value"},
    source="myplugin",
))

# Ecouter / Listen
unsub = ctx.event_bus.on("myplugin.something_happened", my_handler)
```

Convention : `<plugin_name>.<event>` pour les evenements custom.

### Ecouter tous les evenements / Subscribe to All Events

```python
unsub = ctx.event_bus.on_all(my_handler)
```

---

## Configuration

Utiliser `pydantic-settings` avec le prefixe `TUNE_<NAME>_*` :

```python
from pydantic_settings import BaseSettings

class MyPluginSettings(BaseSettings):
    api_key: str = ""
    max_retries: int = 3
    buffer_ms: int = 500

    model_config = {"env_prefix": "TUNE_MYPLUGIN_"}
```

Configuration par variables d'environnement ou `.env` :

```bash
TUNE_MYPLUGIN_API_KEY=secret123
TUNE_MYPLUGIN_BUFFER_MS=1000
```

Retourner la classe dans `config_schema()` pour l'introspection API :

```python
def config_schema(self):
    return MyPluginSettings
```

---

## Exemple : Type de sortie custom / Example: Custom Output Type

```python
from tune_server.outputs.base import OutputTarget
from tune_server.plugins.api import OutputTypeSpec
from tune_server.audio.formats import AudioCapabilities
from tune_server.models import AudioFormat

class IcecastOutput(OutputTarget):
    """Sends audio to an Icecast server."""

    async def start(self, stream_url: str, metadata: dict) -> None:
        # connect to Icecast, start streaming
        ...

    async def stop(self) -> None:
        ...


async def icecast_factory(device_id: str | None) -> IcecastOutput:
    return IcecastOutput(mount_point=device_id or "/stream")


class IcecastPlugin:
    name = "icecast"
    version = "0.1.0"

    async def setup(self, ctx) -> None:
        ctx.register_output_type(OutputTypeSpec(
            name="icecast",
            factory=icecast_factory,
            capabilities=AudioCapabilities(
                formats={AudioFormat.MP3, AudioFormat.OGG, AudioFormat.OPUS},
                max_sample_rate=48000,
                max_bit_depth=16,
            ),
            description="Stream to Icecast server",
            icon="antenna.radiowaves.left.and.right",
        ))

    async def teardown(self) -> None:
        pass

    def config_schema(self):
        return None
```

---

## Exemple : Ecouteur d'evenements (scrobbler) / Example: Event Listener (Scrobbler)

```python
import time
from tune_server.event_bus import Event, EventType

class ScrobblerPlugin:
    name = "scrobbler"
    version = "0.1.0"

    def __init__(self):
        self._unsubs = []
        self._current_track = None
        self._started_at = 0

    async def setup(self, ctx) -> None:
        self._unsubs.append(
            ctx.event_bus.on(EventType.PLAYBACK_STARTED, self._on_start)
        )
        self._unsubs.append(
            ctx.event_bus.on(EventType.PLAYBACK_STOPPED, self._on_stop)
        )

    async def _on_start(self, event: Event) -> None:
        self._current_track = event.data
        self._started_at = time.time()

    async def _on_stop(self, event: Event) -> None:
        if self._current_track and time.time() - self._started_at > 30:
            await self._submit_scrobble(self._current_track)
        self._current_track = None

    async def _submit_scrobble(self, track_data: dict) -> None:
        # POST to scrobbling API
        ...

    async def teardown(self) -> None:
        for unsub in self._unsubs:
            unsub()

    def config_schema(self):
        return None
```

---

## API de gestion des plugins / Plugin Management API

| Methode | Endpoint | Description |
|---------|----------|-------------|
| GET | `/api/v1/system/plugins` | Lister tous les plugins avec statut |
| GET | `/api/v1/system/plugins/{name}` | Detail d'un plugin |
| POST | `/api/v1/system/plugins/{name}/disable` | Desactiver (ignore au prochain boot) |
| POST | `/api/v1/system/plugins/{name}/enable` | Reactiver |

Reponse type :

```json
[
  {
    "name": "lyrics",
    "version": "0.1.0",
    "description": "Synchronized lyrics with karaoke sync",
    "status": "active",
    "output_types": [],
    "routes": ["/api/v1/lyrics", "/api/v1/lyrics/search"],
    "hooks_count": 0,
    "has_config": true
  }
]
```

---

## Compatibilite PROTOCOL_VERSION / Protocol Compatibility

Le serveur declare `PROTOCOL_VERSION = "1"`. En cas de changement cassant de l'API plugin
(signatures, methodes supprimees), la version est incrementee.

Verifier dans `setup()` :

```python
from tune_server.plugins.api import PROTOCOL_VERSION

async def setup(self, ctx):
    if PROTOCOL_VERSION != "1":
        raise RuntimeError(f"Incompatible protocol: {PROTOCOL_VERSION}")
```

---

## Publication sur PyPI / Publishing to PyPI

### Conventions de nommage / Naming Conventions

| Element | Convention | Exemple |
|---------|------------|---------|
| Package PyPI | `tune-plugin-<name>` | `tune-plugin-snapcast` |
| Entry point key | `<name>` | `snapcast` |
| Variables env | `TUNE_<NAME>_*` | `TUNE_SNAPCAST_HOST` |
| Tables DB | `<name>_*` | `snapcast_clients` |

### pyproject.toml complet

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "tune-plugin-myplugin"
version = "0.1.0"
description = "My Tune Server plugin"
requires-python = ">=3.11"
license = "MIT"
dependencies = [
    "tune-server>=0.7.90",
]

[project.entry-points."tune_server.plugins"]
myplugin = "myplugin:MyPlugin"
```

### Publier

```bash
pip install build twine
python -m build
twine upload dist/*
```

---

## Bonnes pratiques / Best Practices

- **Pas de travail dans `__init__()`** — tout dans `setup()`.
- **Utiliser `structlog`** — coherent avec le serveur.
- **Prefixer les tables DB** — `<plugin_name>_*` pour eviter les collisions.
- **Se desabonner dans `teardown()`** — eviter les listeners orphelins.
- **Gerer les erreurs** — un hook qui plante ne doit pas crasher le lecteur.
- **Logger aux bons niveaux** — `debug` pour le verbose, `info` pour le lifecycle, `error` pour les echecs.
- **Pas de monkey-patching** — utiliser uniquement les APIs du `PluginContext`.

---

## Template de plugin / Plugin Template

Un template complet et fonctionnel est disponible dans [`template/`](./template/) :

```bash
cp -r docs/plugins/template/ my-tune-plugin/
# Edit src/my_tune_plugin/plugin.py
pip install -e ./my-tune-plugin
tune-server
```

---

## Voir aussi / See Also

- [`COOKBOOK.md`](./COOKBOOK.md) — Patterns courants (source connector, output, audio processing, notification)
- [`template/`](./template/) — Template complet avec tests
- `/api/v1/system/plugins` — API de gestion runtime

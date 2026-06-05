# Plugin Cookbook — Patterns courants / Common Patterns

Recettes pretes a l'emploi pour les cas d'usage les plus frequents.
Ready-to-use recipes for the most common use cases.

---

## 1. Source Connector Plugin (connecteur de streaming)

Ajouter un service de streaming (ex: SoundCloud, Bandcamp).
Le plugin expose des routes API pour chercher, naviguer et obtenir des URLs de stream.

```python
"""tune-plugin-soundcloud — SoundCloud connector."""
from __future__ import annotations

import aiohttp
import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic_settings import BaseSettings

from tune_server.plugins.api import PluginContext

logger = structlog.get_logger()
router = APIRouter(tags=["soundcloud"])


class SoundCloudSettings(BaseSettings):
    client_id: str = ""
    client_secret: str = ""

    model_config = {"env_prefix": "TUNE_SOUNDCLOUD_"}


class SoundCloudPlugin:
    name = "soundcloud"
    version = "0.1.0"
    description = "SoundCloud streaming connector"

    def __init__(self):
        self._settings = SoundCloudSettings()
        self._session: aiohttp.ClientSession | None = None
        self._unsubs: list = []

    async def setup(self, ctx: PluginContext) -> None:
        if not self._settings.client_id:
            logger.warning("soundcloud_no_client_id")
            return

        self._session = aiohttp.ClientSession()
        ctx.register_router(router)

    async def teardown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def config_schema(self):
        return SoundCloudSettings


@router.get("/soundcloud/search")
async def search(q: str = Query(..., min_length=1)):
    """Search SoundCloud tracks."""
    # Implementation: call SoundCloud API, return normalized results
    ...


@router.get("/soundcloud/stream/{track_id}")
async def get_stream_url(track_id: str):
    """Get a streamable URL for a SoundCloud track."""
    # Implementation: resolve stream URL via API
    ...
```

**Points cles** :
- Creer une `aiohttp.ClientSession` dans `setup()`, la fermer dans `teardown()`.
- Valider les credentials au demarrage, logger un warning si absents.
- Exposer `/search` et `/stream/{id}` minimum pour l'integration UI.

---

## 2. Output Type Plugin (type de sortie audio)

Ajouter un nouveau type de sortie (ex: Snapcast, Icecast, Roon Bridge).

```python
"""tune-plugin-icecast — Icecast streaming output."""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog
from pydantic_settings import BaseSettings

from tune_server.audio.formats import AudioCapabilities
from tune_server.models import AudioFormat
from tune_server.outputs.base import OutputTarget
from tune_server.plugins.api import OutputTypeSpec, PluginContext

logger = structlog.get_logger()


class IcecastSettings(BaseSettings):
    host: str = "localhost"
    port: int = 8000
    mount: str = "/stream"
    password: str = "hackme"
    bitrate: int = 320

    model_config = {"env_prefix": "TUNE_ICECAST_"}


class IcecastOutput(OutputTarget):
    """Envoie l'audio vers un serveur Icecast."""

    def __init__(self, settings: IcecastSettings, mount: str):
        self._settings = settings
        self._mount = mount
        self._connected = False

    async def start(self, stream_url: str, metadata: dict) -> None:
        # Connecter au serveur Icecast, envoyer les headers SOURCE
        logger.info("icecast_connected", mount=self._mount)
        self._connected = True

    async def write(self, data: bytes) -> None:
        # Ecrire les donnees audio encodees
        if not self._connected:
            return
        ...

    async def stop(self) -> None:
        self._connected = False
        logger.info("icecast_disconnected", mount=self._mount)

    async def set_metadata(self, title: str, artist: str = "") -> None:
        # Mettre a jour les metadonnees Icecast (ICY)
        ...


class IcecastPlugin:
    name = "icecast"
    version = "0.1.0"
    description = "Stream audio to Icecast server"

    def __init__(self):
        self._settings = IcecastSettings()

    async def setup(self, ctx: PluginContext) -> None:
        async def factory(device_id: Optional[str]) -> IcecastOutput:
            mount = device_id or self._settings.mount
            return IcecastOutput(self._settings, mount)

        ctx.register_output_type(OutputTypeSpec(
            name="icecast",
            factory=factory,
            capabilities=AudioCapabilities(
                formats={AudioFormat.MP3, AudioFormat.OGG, AudioFormat.OPUS},
                max_sample_rate=48000,
                max_bit_depth=16,
            ),
            description="Icecast streaming output",
            icon="antenna.radiowaves.left.and.right",
        ))

    async def teardown(self) -> None:
        pass

    def config_schema(self):
        return IcecastSettings
```

**Points cles** :
- `OutputTypeSpec.factory` est un async callable `(Optional[str]) -> OutputTarget`.
- `AudioCapabilities` declare ce que la sortie peut recevoir — le pipeline audio s'adapte.
- L'`icon` est un nom SF Symbols (Apple) ou Material Icons.

---

## 3. Audio Processing Plugin (traitement audio)

Intercepter le flux audio pour appliquer un traitement (EQ, normalisation, DRC).
Utilise les player hooks `BEFORE_TRACK` / `AFTER_TRACK`.

```python
"""tune-plugin-loudnorm — Loudness normalization via ReplayGain."""
from __future__ import annotations

import structlog
from tune_server.event_bus import Event, EventType
from tune_server.playback.player import PlayerHookEvent
from tune_server.plugins.api import PluginContext

logger = structlog.get_logger()


class LoudnormPlugin:
    name = "loudnorm"
    version = "0.1.0"
    description = "ReplayGain loudness normalization"

    def __init__(self):
        self._unsubs = []

    async def setup(self, ctx: PluginContext) -> None:
        # Hook qui s'execute avant chaque piste
        ctx.register_player_hook(
            PlayerHookEvent.BEFORE_TRACK,
            self._apply_gain,
        )

        # Ecouter aussi les changements de config
        self._unsubs.append(
            ctx.event_bus.on(EventType.ZONE_UPDATED, self._on_zone_update)
        )

    async def _apply_gain(self, zone_id: int, track) -> None:
        """Lire le tag ReplayGain de la piste et ajuster le gain."""
        rg_gain = getattr(track, "replaygain_track_gain", None)
        if rg_gain:
            logger.debug("applying_replaygain", zone=zone_id, gain=rg_gain)
            # Le gain est applique via la pipeline audio FFmpeg
            # -af volume=<gain>dB

    async def _on_zone_update(self, event: Event) -> None:
        """Reagir aux changements de configuration de zone."""
        ...

    async def teardown(self) -> None:
        for unsub in self._unsubs:
            unsub()

    def config_schema(self):
        return None
```

**Points cles** :
- Les player hooks se declenchent pour TOUTES les zones.
- `BEFORE_TRACK` permet d'ajuster les parametres avant le demarrage audio.
- Bien `unsub()` dans `teardown()` pour les listeners event bus.

---

## 4. Notification Plugin (webhook / MQTT)

Emettre des notifications vers des systemes externes a chaque evenement de lecture.

```python
"""tune-plugin-webhook — Webhook notifications on playback events."""
from __future__ import annotations

import aiohttp
import structlog
from pydantic_settings import BaseSettings

from tune_server.event_bus import Event, EventType
from tune_server.plugins.api import PluginContext

logger = structlog.get_logger()


class WebhookSettings(BaseSettings):
    url: str = ""
    secret: str = ""
    events: str = "playback.started,playback.stopped"

    model_config = {"env_prefix": "TUNE_WEBHOOK_"}


class WebhookPlugin:
    name = "webhook"
    version = "0.1.0"
    description = "HTTP webhook notifications"

    def __init__(self):
        self._settings = WebhookSettings()
        self._session: aiohttp.ClientSession | None = None
        self._unsubs: list = []

    async def setup(self, ctx: PluginContext) -> None:
        if not self._settings.url:
            logger.info("webhook_disabled_no_url")
            return

        self._session = aiohttp.ClientSession()

        # S'abonner aux evenements configurees
        event_names = [e.strip() for e in self._settings.events.split(",")]
        for event_name in event_names:
            try:
                event_type = EventType(event_name)
                unsub = ctx.event_bus.on(event_type, self._send_webhook)
                self._unsubs.append(unsub)
            except ValueError:
                # Evenement custom (string)
                unsub = ctx.event_bus.on(event_name, self._send_webhook)
                self._unsubs.append(unsub)

    async def _send_webhook(self, event: Event) -> None:
        if not self._session:
            return
        try:
            headers = {}
            if self._settings.secret:
                headers["X-Tune-Secret"] = self._settings.secret

            await self._session.post(
                self._settings.url,
                json={
                    "event": str(event.type),
                    "data": event.data if isinstance(event.data, dict) else {},
                    "source": event.source,
                },
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            )
        except Exception:
            logger.warning("webhook_failed", url=self._settings.url)

    async def teardown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        if self._session and not self._session.closed:
            await self._session.close()

    def config_schema(self):
        return WebhookSettings
```

**Variante MQTT** (Home Assistant, Node-RED) :

```python
# Meme pattern, mais avec aiomqtt au lieu de aiohttp
import aiomqtt

class MqttPlugin:
    name = "mqtt"
    version = "0.1.0"

    async def setup(self, ctx: PluginContext) -> None:
        self._client = aiomqtt.Client(hostname="localhost")
        await self._client.__aenter__()
        self._unsubs = [
            ctx.event_bus.on(EventType.PLAYBACK_STARTED, self._publish),
            ctx.event_bus.on(EventType.PLAYBACK_STOPPED, self._publish),
        ]

    async def _publish(self, event: Event) -> None:
        import json
        await self._client.publish(
            f"tune/{event.type}",
            json.dumps(event.data if isinstance(event.data, dict) else {}),
        )

    async def teardown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        await self._client.__aexit__(None, None, None)
```

**Points cles** :
- Toujours un timeout sur les appels HTTP sortants.
- Ne jamais bloquer le lecteur — logger et continuer en cas d'erreur.
- Gestion propre du cycle de vie `aiohttp.ClientSession` / `aiomqtt.Client`.

---

## 5. UI Widget Plugin (widget interface)

Ajouter un widget dans le web client via une route API + metadata.

```python
"""Plugin qui expose des widgets pour le dashboard."""
from fastapi import APIRouter

router = APIRouter(tags=["dashboard"])


@router.get("/widgets/now-playing-lyrics")
async def now_playing_lyrics(zone_id: int):
    """Widget affichant les paroles synchronisees."""
    return {
        "widget": "lyrics",
        "html": "<div class='lyrics-widget'>...</div>",
        "refresh_ms": 1000,
    }


class DashboardPlugin:
    name = "dashboard_widgets"
    version = "0.1.0"

    async def setup(self, ctx) -> None:
        ctx.register_router(router)

    async def teardown(self) -> None:
        pass

    def config_schema(self):
        return None
```

---

## Patterns transverses / Cross-Cutting Patterns

### Stocker des donnees persistantes / Persistent Storage

Utiliser SQLite avec `aiosqlite` pour les donnees plugin :

```python
import aiosqlite
from pathlib import Path

class MyPlugin:
    async def setup(self, ctx) -> None:
        self._db_path = Path.home() / ".tune" / "plugins" / "myplugin" / "data.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS myplugin_cache (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL
            )
        """)
        await self._db.commit()

    async def teardown(self) -> None:
        if self._db:
            await self._db.close()
```

### Tache de fond / Background Task

Lancer un polling ou un worker async :

```python
class MyPlugin:
    async def setup(self, ctx) -> None:
        self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._do_work()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("poll_error")
            await asyncio.sleep(30)

    async def teardown(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
```

### Configuration dynamique / Dynamic Config

Recharger la config sans redemarrer :

```python
@router.post("/myplugin/config")
async def update_config(new_config: dict):
    # Valider avec le schema pydantic
    validated = MyPluginSettings(**new_config)
    # Appliquer en memoire
    plugin_instance._settings = validated
    return {"status": "updated"}
```

"""Radio metadata polling — RadioFrance API for FIP and variants."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import aiohttp
import structlog

from tune_server.event_bus import Event, EventBus, EventType

logger = structlog.get_logger()

RADIOFRANCE_API = "https://api.radiofrance.fr/livemeta/live/7/webrf_{station}_player"
POLL_INTERVAL = 15  # seconds


@dataclass
class NowPlaying:
    title: str
    artist: str
    cover_url: str | None = None


def _detect_station(stream_url: str) -> str | None:
    """Detect RadioFrance station from stream URL."""
    url = stream_url.lower()
    if "fip" not in url and "radiofrance" not in url:
        return None

    variants = {
        "rock": "fip_rock", "jazz": "fip_jazz", "electro": "fip_electro",
        "monde": "fip_world", "world": "fip_world", "reggae": "fip_reggae",
        "nouveau": "fip_nouveautes", "groove": "fip_groove", "pop": "fip_pop",
        "metal": "fip_metal", "hip": "fip_hiphop",
    }
    for key, station in variants.items():
        if key in url:
            return station
    if "fip" in url:
        return "fip"
    return None


async def _fetch_radiofrance(station: str) -> NowPlaying | None:
    """Fetch current track from RadioFrance livemeta API."""
    url = RADIOFRANCE_API.format(station=station)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                now = data.get("now", {})
                title = now.get("firstLine", "")
                artist = now.get("secondLine", "")
                if not title:
                    return None
                cover_uuid = now.get("cover")
                cover_url = None
                if cover_uuid:
                    cover_url = f"https://www.radiofrance.fr/s3/cruiser-production/{cover_uuid}/200x200_rf_omm_0001903544_dnc.0099.jpg"
                return NowPlaying(title=title, artist=artist, cover_url=cover_url)
    except Exception:
        return None


class RadioMetadataPoller:
    """Polls radio metadata APIs and emits PLAYBACK_METADATA events."""

    def __init__(self, event_bus: EventBus, zone_id: int, track_callback=None) -> None:
        self._event_bus = event_bus
        self._zone_id = zone_id
        self._track_callback = track_callback  # _make_icy_callback result
        self._task: asyncio.Task | None = None
        self._last_title: str | None = None

    def start(self, stream_url: str) -> None:
        self.stop()
        station = _detect_station(stream_url)
        if not station:
            logger.debug("radio_metadata_no_provider", url=stream_url[:60])
            return
        self._task = asyncio.create_task(self._poll_loop(station))
        logger.info("radio_metadata_poller_started", station=station, zone_id=self._zone_id)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
            self._last_title = None

    async def _poll_loop(self, station: str) -> None:
        try:
            while True:
                np = await _fetch_radiofrance(station)
                if np and np.title != self._last_title:
                    self._last_title = np.title
                    logger.info("radio_metadata_update",
                                title=np.title, artist=np.artist,
                                zone_id=self._zone_id)

                    # Update track in player memory (so zone API reflects it)
                    if self._track_callback:
                        self._track_callback({
                            "title": np.title,
                            "artist": np.artist,
                            "cover_url": np.cover_url,
                        })

                    self._event_bus.emit_nowait(Event(
                        type=EventType.PLAYBACK_METADATA,
                        data={
                            "zone_id": self._zone_id,
                            "title": np.title,
                            "artist_name": np.artist,
                            "album_title": "FIP",
                            "cover_path": np.cover_url,
                            "source": "radio",
                        },
                        source="radio_metadata",
                    ))
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("radio_metadata_poller_error")

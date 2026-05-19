"""BluOS (Bluesound) audio output.

Audio is streamed via HTTP URL (same pull model as DLNA/Chromecast).
BluOS devices expose an HTTP API on port 11000.
"""

from __future__ import annotations

import asyncio
from typing import Optional
from xml.etree import ElementTree

import aiohttp
import structlog

from tune_server.audio.formats import AudioCapabilities, AudioFormat, BLUOS_CAPABILITIES
from tune_server.models import AudioStreamInfo, Track
from tune_server.outputs.base import OutputTarget

logger = structlog.get_logger()

_BLUOS_DIRECT_FORMATS = {AudioFormat.FLAC, AudioFormat.MP3, AudioFormat.AAC, AudioFormat.OGG, AudioFormat.WAV}

_MIME_MAP = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "aac": "audio/mp4",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
}


class BluosOutput(OutputTarget):
    """Stream audio to a BluOS device via its HTTP API."""

    def __init__(
        self,
        host: str,
        streamer,
        server_ip: str,
        port: int = 11000,
        device_name: str = "BluOS",
    ) -> None:
        self._host = host
        self._port = port
        self._streamer = streamer
        self._server_ip = server_ip
        self._name = device_name
        self._available = True
        self._stream_id: str | None = None
        self._direct_url: bool = False
        self._session: aiohttp.ClientSession | None = None
        self._last_totlen_ms: int = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> AudioCapabilities:
        return BLUOS_CAPABILITIES

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def is_direct_url(self) -> bool:
        return self._direct_url

    @property
    def has_pending_stream(self) -> bool:
        return self._stream_id is not None

    def supports_direct_url(self, track: Track) -> bool:
        # Always route through the HTTP streamer so ICY metadata headers
        # (title, artist, cover) are served.  BluOS /Play has no metadata
        # parameters — ICY is the only channel for track info.
        return False

    def _base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def _api_get(self, path: str, **params) -> str:
        """Send a GET request to the BluOS API and return the response text."""
        session = await self._get_session()
        url = f"{self._base_url()}/{path}"
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.text()

    def _resolve_cover_url(self, track: Track | None) -> str | None:
        """Build a cover art URL reachable from the BluOS player."""
        if not track or not track.cover_path:
            return None
        cover = track.cover_path
        if cover.startswith("http"):
            # Proxy through the Tune API so the player always gets a
            # reachable HTTP URL (CDN tokens may expire or require HTTPS).
            from urllib.parse import quote
            return f"http://{self._server_ip}:8888/api/v1/library/artwork/proxy?url={quote(cover, safe='')}"
        if cover.startswith("/"):
            return f"http://{self._server_ip}:8888{cover}"
        filename = cover.split("/")[-1]
        return f"http://{self._server_ip}:8888/api/v1/library/artwork/{filename}"

    async def start(self, stream_info: AudioStreamInfo, track: Optional[Track] = None) -> None:
        if self._stream_id:
            self._streamer.remove_session(self._stream_id)
            self._stream_id = None
        self._direct_url = False

        title = track.title if track else "Audio"
        artist = track.artist_name if track else ""
        album = track.album_title if track else ""
        cover_url = self._resolve_cover_url(track)

        # Always route through the HTTP streamer for ICY metadata headers.
        file_path = track.file_path if track else None
        self._stream_id = self._streamer.create_session(stream_info, file_path)
        url = self._streamer.get_stream_url(self._stream_id, self._server_ip)
        self._streamer.set_session_metadata(
            self._stream_id,
            title=title,
            artist=artist,
            album=album,
            cover_url=cover_url,
        )

        # BluOS /Play only accepts 'url' (and 'seek') — title/artist/album
        # are NOT API parameters (they are read-only /Status attributes).
        # Metadata is delivered via ICY headers in the HTTP stream instead.
        try:
            await self._api_get("Play", url=url)
            logger.info("bluos_playing", device=self._name, title=title, artist=artist, url=url[:80])
        except Exception:
            logger.exception("bluos_start_error", device=self._name)
            raise

    async def write(self, data: bytes) -> None:
        if self._direct_url:
            return
        if self._stream_id:
            session = self._streamer.get_session(self._stream_id)
            if session:
                await session.put(data)

    async def flush(self) -> None:
        pass

    async def pause(self) -> None:
        try:
            await self._api_get("Pause")
        except Exception:
            logger.debug("bluos_pause_error", device=self._name)

    async def resume(self) -> None:
        try:
            await self._api_get("Play")
        except Exception:
            logger.debug("bluos_resume_error", device=self._name)

    async def stop(self) -> None:
        try:
            await self._api_get("Stop")
        except Exception:
            pass
        if self._stream_id:
            self._streamer.remove_session(self._stream_id)
            self._stream_id = None
        self._direct_url = False

    async def set_volume(self, volume: float) -> None:
        try:
            level = int(max(0, min(100, volume * 100)))
            await self._api_get("Volume", level=str(level))
        except Exception:
            logger.debug("bluos_volume_error", device=self._name)

    async def seek(self, position_ms: int) -> bool:
        try:
            seconds = position_ms / 1000.0
            await self._api_get("Play", seek=str(int(seconds)))
            logger.info("bluos_seek", device=self._name, position_ms=position_ms)
            return True
        except Exception:
            logger.debug("bluos_seek_error", device=self._name)
            return False

    async def get_position_ms(self) -> int:
        try:
            text = await self._api_get("Status")
            root = ElementTree.fromstring(text)

            state_el = root.find("state")
            state = (state_el.text or "").strip().lower() if state_el is not None else ""

            if state in ("stop", ""):
                return -2

            totlen_el = root.find("totlen")
            if totlen_el is not None and totlen_el.text:
                self._last_totlen_ms = int(float(totlen_el.text) * 1000)

            secs_el = root.find("secs")
            if secs_el is not None and secs_el.text:
                return int(float(secs_el.text) * 1000)
        except Exception:
            pass
        return -1

    @property
    def reported_duration_ms(self) -> int:
        return self._last_totlen_ms

    async def set_next_track(self, stream_info: AudioStreamInfo, track: Track) -> bool:
        return False

    async def close(self) -> None:
        await self.stop()
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

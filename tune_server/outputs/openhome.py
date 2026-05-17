"""OpenHome output target.

Streams audio to OpenHome-compatible renderers (Linn, Naim, Auralic,
Lumin, dCS, Cambridge Audio, etc.) using the Playlist service for
full queue sync and native gapless playback.

Delegates all SOAP communication to :class:`OpenHomeClient` in
``oh_client.py``.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from tune_server.audio.formats import AudioCapabilities, AudioFormat
from tune_server.models import AudioStreamInfo, Track
from tune_server.outputs.base import OutputTarget
from tune_server.outputs.oh_client import OpenHomeClient

logger = structlog.get_logger()

_OPENHOME_CAPABILITIES = AudioCapabilities(
    formats={AudioFormat.FLAC, AudioFormat.WAV, AudioFormat.MP3, AudioFormat.AAC},
    max_sample_rate=384000,
    max_bit_depth=32,
    supports_gapless=True,
)


# ---------------------------------------------------------------------------
# DIDL-Lite helper
# ---------------------------------------------------------------------------

def _xml_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _build_didl(uri: str, title: str = "", artist: str = "", album: str = "",
                duration_ms: int = 0, cover_url: str = "") -> str:
    dur_str = ""
    if duration_ms > 0:
        s = duration_ms // 1000
        dur_str = f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"

    art_xml = f"<upnp:albumArtURI>{_xml_escape(cover_url)}</upnp:albumArtURI>" if cover_url else ""
    dur_xml = f' duration="{dur_str}"' if dur_str else ""

    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        f'<item id="0" parentID="0" restricted="1">'
        f'<dc:title>{_xml_escape(title)}</dc:title>'
        f'<dc:creator>{_xml_escape(artist)}</dc:creator>'
        f'<upnp:album>{_xml_escape(album)}</upnp:album>'
        f'{art_xml}'
        f'<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<res protocolInfo="http-get:*:audio/flac:*"{dur_xml}>{_xml_escape(uri)}</res>'
        f'</item></DIDL-Lite>'
    )


# ---------------------------------------------------------------------------
# OutputTarget implementation
# ---------------------------------------------------------------------------

class OpenHomeOutput(OutputTarget):
    """OpenHome renderer output backed by :class:`OpenHomeClient`."""

    def __init__(
        self,
        device_name: str,
        service_urls: dict[str, str],
        server_ip: str,
        streamer: object,
        base_url: str = "",
    ) -> None:
        self._client = OpenHomeClient(service_urls, device_name=device_name)
        self._device_name = device_name
        self._server_ip = server_ip
        self._streamer = streamer
        self._base_url = base_url
        self._available = True
        self._volume: float = 0.5
        self._current_oh_id: int | None = None
        self._playlist_ids: list[int] = []

    @property
    def name(self) -> str:
        return self._device_name

    @property
    def capabilities(self) -> AudioCapabilities:
        return _OPENHOME_CAPABILITIES

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def is_direct_url(self) -> bool:
        return True

    def supports_direct_url(self, track: Track) -> bool:
        return True

    # -- helpers ---------------------------------------------------------------

    def _build_stream_url(self, track: Track) -> str:
        if track.file_path and (track.file_path.startswith("http://") or
                                track.file_path.startswith("https://")):
            return track.file_path
        if track.id:
            return f"http://{self._server_ip}:8888/api/v1/library/tracks/{track.id}/audio"
        return track.file_path or ""

    # -- OutputTarget interface ------------------------------------------------

    async def start(self, stream_info: AudioStreamInfo, track: Optional[Track] = None) -> None:
        if not track:
            logger.error("openhome_no_track", device=self._device_name)
            return

        await self.stop()
        await self._client.select_playlist_source()
        await asyncio.sleep(0.3)

        uri = self._build_stream_url(track)
        metadata = _build_didl(
            uri=uri,
            title=track.title or "",
            artist=track.artist_name or "",
            album=track.album_title or "",
            duration_ms=track.duration_ms or 0,
            cover_url=track.cover_path or "",
        )

        await self._client.playlist_delete_all()
        new_id = await self._client.playlist_insert(0, uri, metadata)
        if new_id is not None:
            self._current_oh_id = new_id
            self._playlist_ids = [new_id]
            await self._client.playlist_seek_id(new_id)
            await self._client.transport_play()
            self._available = True
            logger.info("openhome_play_started", device=self._device_name,
                        track=track.title, uri=uri[:80])
        else:
            logger.error("openhome_insert_failed", device=self._device_name)
            self._available = False

    async def write(self, data: bytes) -> None:
        pass

    async def flush(self) -> None:
        pass

    async def pause(self) -> None:
        await self._client.transport_pause()

    async def resume(self) -> None:
        await self._client.transport_play()

    async def stop(self) -> None:
        await self._client.transport_stop()
        self._current_oh_id = None

    async def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        await self._client.volume_set(int(self._volume * 100))

    async def get_position_ms(self) -> int:
        dur, pos = await self._client.time_get()
        return pos * 1000 if pos >= 0 else -1

    async def seek(self, position_ms: int) -> bool:
        seconds = position_ms // 1000
        await self._client.transport_seek_seconds(seconds)
        return True

    async def set_next_track(self, stream_info: AudioStreamInfo, track: Track) -> bool:
        """Insert next track after the current one for gapless playback."""
        uri = self._build_stream_url(track)
        metadata = _build_didl(
            uri=uri,
            title=track.title or "",
            artist=track.artist_name or "",
            album=track.album_title or "",
            duration_ms=track.duration_ms or 0,
            cover_url=track.cover_path or "",
        )
        after_id = self._current_oh_id or 0
        new_id = await self._client.playlist_insert(after_id, uri, metadata)
        if new_id is not None:
            self._playlist_ids.append(new_id)
            logger.debug("openhome_next_track_set", device=self._device_name,
                         track=track.title)
            return True
        return False

    async def sync_queue(self, tracks: list[Track]) -> None:
        """Push entire Tune queue to device Playlist."""
        await self._client.playlist_delete_all()
        self._playlist_ids = []
        last_id = 0
        for track in tracks:
            uri = self._build_stream_url(track)
            metadata = _build_didl(
                uri=uri,
                title=track.title or "",
                artist=track.artist_name or "",
                album=track.album_title or "",
                duration_ms=track.duration_ms or 0,
                cover_url=track.cover_path or "",
            )
            new_id = await self._client.playlist_insert(last_id, uri, metadata)
            if new_id is not None:
                self._playlist_ids.append(new_id)
                last_id = new_id
            else:
                logger.warning("openhome_sync_insert_failed", track=track.title)
                break

        if self._playlist_ids:
            self._current_oh_id = self._playlist_ids[0]
            await self._client.playlist_seek_id(self._current_oh_id)
            await self._client.transport_play()
            logger.info("openhome_queue_synced", device=self._device_name,
                        tracks=len(self._playlist_ids))

    async def close(self) -> None:
        await self.stop()
        await self._client.close()

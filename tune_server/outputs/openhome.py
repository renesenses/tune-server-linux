"""OpenHome output target.

Streams audio to OpenHome-compatible renderers (Linn, Naim, Auralic,
Lumin, dCS, Cambridge Audio, etc.) using the Playlist service for
full queue sync and native gapless playback.

Phase 1: push tracks via Playlist Insert + SeekId.
Phase 2: full queue sync — mirror the Tune queue into the device playlist.
"""
from __future__ import annotations

import asyncio
from typing import Optional
from xml.etree import ElementTree

import aiohttp
import structlog

from tune_server.audio.formats import AudioCapabilities, AudioFormat
from tune_server.models import AudioStreamInfo, Track
from tune_server.outputs.base import OutputTarget

logger = structlog.get_logger()

_SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_OH_PLAYLIST_TYPE = "urn:av-openhome-org:service:Playlist:1"
_OH_TRANSPORT_TYPE = "urn:av-openhome-org:service:Transport:1"
_OH_VOLUME_TYPE = "urn:av-openhome-org:service:Volume:1"
_OH_TIME_TYPE = "urn:av-openhome-org:service:Time:1"
_OH_PRODUCT_TYPE = "urn:av-openhome-org:service:Product:1"
_OH_INFO_TYPE = "urn:av-openhome-org:service:Info:1"

_OPENHOME_CAPABILITIES = AudioCapabilities(
    formats={AudioFormat.FLAC, AudioFormat.WAV, AudioFormat.MP3, AudioFormat.AAC},
    max_sample_rate=384000,
    max_bit_depth=32,
    supports_gapless=True,
)


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


def _xml_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


class OpenHomeOutput(OutputTarget):
    """OpenHome renderer output using direct UPnP SOAP calls."""

    def __init__(
        self,
        device_name: str,
        service_urls: dict[str, str],
        server_ip: str,
        streamer: object,
        base_url: str = "",
    ) -> None:
        self._device_name = device_name
        self._urls = service_urls
        self._server_ip = server_ip
        self._streamer = streamer
        self._base_url = base_url
        self._available = True
        self._volume: float = 0.5
        self._current_oh_id: int | None = None
        self._playlist_ids: list[int] = []
        self._session: aiohttp.ClientSession | None = None

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

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _soap_call(self, url: str, service_type: str, action: str,
                         args: dict[str, str] | None = None) -> ElementTree.Element | None:
        body_args = ""
        if args:
            for k, v in args.items():
                body_args += f"<{k}>{_xml_escape(v)}</{k}>"

        envelope = (
            f'<?xml version="1.0" encoding="utf-8"?>'
            f'<s:Envelope xmlns:s="{_SOAP_NS}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{service_type}">{body_args}</u:{action}>'
            f'</s:Body></s:Envelope>'
        )

        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service_type}#{action}"',
        }

        session = await self._get_session()
        try:
            async with session.post(url, data=envelope, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.debug("openhome_soap_error", action=action, status=resp.status, body=text[:200])
                    return None
                text = await resp.text()
                return ElementTree.fromstring(text)
        except Exception as exc:
            logger.debug("openhome_soap_exception", action=action, error=str(exc))
            return None

    def _extract_value(self, root: ElementTree.Element, tag: str) -> str | None:
        for elem in root.iter():
            etag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if etag == tag:
                return elem.text
        return None

    # -------------------------------------------------------------------------
    # Product source selection
    # -------------------------------------------------------------------------

    async def _select_playlist_source(self) -> None:
        url = self._urls.get("product")
        if not url:
            return
        result = await self._soap_call(url, _OH_PRODUCT_TYPE, "SourceXml")
        if result is None:
            return
        xml_str = self._extract_value(result, "Value")
        if not xml_str:
            return
        try:
            sources = ElementTree.fromstring(xml_str)
        except ElementTree.ParseError:
            return
        for idx, source in enumerate(sources):
            stype = ""
            for child in source:
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag == "Type":
                    stype = (child.text or "").strip()
            if stype == "Playlist":
                await self._soap_call(url, _OH_PRODUCT_TYPE, "SetSourceIndex",
                                       {"Value": str(idx)})
                logger.debug("openhome_source_set", source="Playlist", index=idx)
                return
        logger.debug("openhome_no_playlist_source")

    # -------------------------------------------------------------------------
    # Playlist service — Phase 2: full queue sync
    # -------------------------------------------------------------------------

    async def _playlist_delete_all(self) -> None:
        url = self._urls.get("playlist")
        if url:
            await self._soap_call(url, _OH_PLAYLIST_TYPE, "DeleteAll")
            self._playlist_ids = []

    async def _playlist_insert(self, after_id: int, uri: str, metadata: str) -> int | None:
        url = self._urls.get("playlist")
        if not url:
            return None
        result = await self._soap_call(url, _OH_PLAYLIST_TYPE, "Insert", {
            "AfterId": str(after_id),
            "Uri": uri,
            "Metadata": metadata,
        })
        if result is None:
            return None
        new_id = self._extract_value(result, "NewId")
        return int(new_id) if new_id else None

    async def _playlist_seek_id(self, track_id: int) -> None:
        url = self._urls.get("playlist")
        if url:
            await self._soap_call(url, _OH_PLAYLIST_TYPE, "SeekId", {"Value": str(track_id)})

    async def _transport_play(self) -> None:
        url = self._urls.get("playlist")
        if url:
            await self._soap_call(url, _OH_PLAYLIST_TYPE, "Play")

    async def _transport_pause(self) -> None:
        url = self._urls.get("playlist")
        if url:
            await self._soap_call(url, _OH_PLAYLIST_TYPE, "Pause")

    async def _transport_stop(self) -> None:
        url = self._urls.get("playlist")
        if url:
            await self._soap_call(url, _OH_PLAYLIST_TYPE, "Stop")

    def _build_stream_url(self, track: Track) -> str:
        if track.file_path and (track.file_path.startswith("http://") or
                                 track.file_path.startswith("https://")):
            return track.file_path
        if track.id:
            return f"http://{self._server_ip}:8888/api/v1/library/tracks/{track.id}/audio"
        return track.file_path or ""

    # -------------------------------------------------------------------------
    # OutputTarget interface
    # -------------------------------------------------------------------------

    async def start(self, stream_info: AudioStreamInfo, track: Optional[Track] = None) -> None:
        if not track:
            logger.error("openhome_no_track", device=self._device_name)
            return

        await self.stop()
        await self._select_playlist_source()
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

        await self._playlist_delete_all()
        new_id = await self._playlist_insert(0, uri, metadata)
        if new_id is not None:
            self._current_oh_id = new_id
            self._playlist_ids = [new_id]
            await self._playlist_seek_id(new_id)
            await self._transport_play()
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
        await self._transport_pause()

    async def resume(self) -> None:
        await self._transport_play()

    async def stop(self) -> None:
        await self._transport_stop()
        self._current_oh_id = None

    async def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        url = self._urls.get("volume")
        if url:
            await self._soap_call(url, _OH_VOLUME_TYPE, "SetVolume",
                                   {"Value": str(int(self._volume * 100))})

    async def get_position_ms(self) -> int:
        url = self._urls.get("time")
        if not url:
            return -1
        result = await self._soap_call(url, _OH_TIME_TYPE, "Time")
        if result is None:
            return -1
        seconds = self._extract_value(result, "TrackDuration")
        pos_seconds = self._extract_value(result, "Seconds")
        if pos_seconds is not None:
            return int(pos_seconds) * 1000
        return -1

    async def seek(self, position_ms: int) -> bool:
        url = self._urls.get("playlist")
        if not url:
            return False
        seconds = position_ms // 1000
        result = await self._soap_call(url, _OH_PLAYLIST_TYPE, "SeekSecondAbsolute",
                                        {"Value": str(seconds)})
        return result is not None

    async def set_next_track(self, stream_info: AudioStreamInfo, track: Track) -> bool:
        """Phase 2 gapless: insert next track after the current one."""
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
        new_id = await self._playlist_insert(after_id, uri, metadata)
        if new_id is not None:
            self._playlist_ids.append(new_id)
            logger.debug("openhome_next_track_set", device=self._device_name, track=track.title)
            return True
        return False

    async def sync_queue(self, tracks: list[Track]) -> None:
        """Phase 2: full queue sync — push entire Tune queue to device Playlist."""
        await self._playlist_delete_all()
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
            new_id = await self._playlist_insert(last_id, uri, metadata)
            if new_id is not None:
                self._playlist_ids.append(new_id)
                last_id = new_id
            else:
                logger.warning("openhome_sync_insert_failed", track=track.title)
                break

        if self._playlist_ids:
            self._current_oh_id = self._playlist_ids[0]
            await self._playlist_seek_id(self._current_oh_id)
            await self._transport_play()
            logger.info("openhome_queue_synced", device=self._device_name,
                         tracks=len(self._playlist_ids))

    async def close(self) -> None:
        await self.stop()
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

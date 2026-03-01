from __future__ import annotations

import asyncio
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

import structlog

from tune_server.audio.formats import DLNA_CAPABILITIES, AudioCapabilities, mime_type_for_format
from tune_server.models import AudioFormat, AudioStreamInfo, Source, Track
from tune_server.outputs.base import OutputTarget
from tune_server.outputs.http_streamer import HttpAudioStreamer

# Formats that DLNA renderers can typically fetch and decode directly from a URL
_DLNA_DIRECT_FORMATS = {AudioFormat.FLAC, AudioFormat.MP3, AudioFormat.AAC}

logger = structlog.get_logger()


def _format_duration(ms: int | None) -> str:
    """Format milliseconds as DLNA duration string (H:MM:SS.mmm)."""
    if not ms or ms <= 0:
        return ""
    total_s, remainder_ms = divmod(ms, 1000)
    hours, remainder_s = divmod(total_s, 3600)
    minutes, seconds = divmod(remainder_s, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{remainder_ms:03d}"


def _build_didl_lite(track: Track, stream_url: str, mime_type: str) -> str:
    """Build DIDL-Lite XML metadata for DLNA."""
    title = xml_escape(track.title or "Unknown")
    artist = xml_escape(track.artist_name or "Unknown Artist")
    album = xml_escape(track.album_title or "Unknown Album")

    # Build res attributes
    res_attrs = f'protocolInfo="http-get:*:{mime_type}:*"'
    duration = _format_duration(track.duration_ms)
    if duration:
        res_attrs += f' duration="{duration}"'
    if track.sample_rate:
        res_attrs += f' sampleFrequency="{track.sample_rate}"'
    if track.bit_depth:
        res_attrs += f' bitsPerSample="{track.bit_depth}"'
    if track.channels:
        res_attrs += f' nrAudioChannels="{track.channels}"'

    # Album art
    art_tag = ""
    if track.cover_path:
        art_url = xml_escape(track.cover_path)
        art_tag = f'<upnp:albumArtURI>{art_url}</upnp:albumArtURI>'

    # Use audioBroadcast class for radio streams
    upnp_class = (
        "object.item.audioItem.audioBroadcast"
        if track.source == Source.RADIO
        else "object.item.audioItem.musicTrack"
    )

    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="1" parentID="0" restricted="1">'
        f'<dc:title>{title}</dc:title>'
        f'<dc:creator>{artist}</dc:creator>'
        f'<upnp:artist>{artist}</upnp:artist>'
        f'<upnp:album>{album}</upnp:album>'
        f'{art_tag}'
        f'<upnp:class>{upnp_class}</upnp:class>'
        f'<res {res_attrs}>{xml_escape(stream_url)}</res>'
        '</item></DIDL-Lite>'
    )


class DlnaOutput(OutputTarget):
    """DLNA/UPnP renderer output using async-upnp-client."""

    def __init__(
        self,
        device: object,  # async_upnp_client.DmrDevice
        streamer: HttpAudioStreamer,
        server_ip: str,
    ) -> None:
        self._device = device
        self._streamer = streamer
        self._server_ip = server_ip
        self._stream_id: str | None = None
        self._direct_url: bool = False
        self._available = True
        self._volume: float = 0.5

    @property
    def name(self) -> str:
        return getattr(self._device, "name", "DLNA Renderer")

    @property
    def capabilities(self) -> AudioCapabilities:
        return DLNA_CAPABILITIES

    @property
    def is_available(self) -> bool:
        return self._available

    def supports_direct_url(self, track: Track) -> bool:
        if not track or not track.file_path:
            return False
        if not (track.file_path.startswith("http://") or track.file_path.startswith("https://")):
            return False
        fmt = AudioFormat(track.format) if track.format else None
        return fmt in _DLNA_DIRECT_FORMATS

    async def start(self, stream_info: AudioStreamInfo, track: Optional[Track] = None) -> None:
        self._direct_url = False

        try:
            # Direct URL passthrough: let the DLNA renderer fetch from the CDN
            if track and self.supports_direct_url(track):
                mime = mime_type_for_format(AudioFormat(track.format))
                metadata = _build_didl_lite(track, track.file_path, mime)

                dmr = self._device
                title = track.title or "Unknown"
                await asyncio.wait_for(
                    dmr.async_set_transport_uri(track.file_path, title, meta_data=metadata), timeout=10
                )
                await asyncio.wait_for(dmr.async_play(), timeout=10)

                self._direct_url = True
                self._available = True
                logger.info("dlna_direct_url_playback", device=self.name, url=track.file_path[:80])
                return

            # Standard flow: stream via local HTTP server
            file_path = track.file_path if track else None
            self._stream_id = self._streamer.create_session(stream_info, file_path)
            stream_url = self._streamer.get_stream_url(self._stream_id, self._server_ip)

            mime = mime_type_for_format(stream_info.format)
            metadata = _build_didl_lite(track, stream_url, mime) if track else ""

            title = track.title if track else "Unknown"
            dmr = self._device
            await asyncio.wait_for(
                dmr.async_set_transport_uri(stream_url, title, meta_data=metadata), timeout=10
            )
            await asyncio.wait_for(dmr.async_play(), timeout=10)

            self._available = True
            logger.info("dlna_playback_started", device=self.name, url=stream_url)
        except Exception:
            logger.exception("dlna_start_error", device=self.name)
            self._available = False

    async def write(self, data: bytes) -> None:
        if self._direct_url:
            return  # Renderer pulls directly from CDN
        # For DLNA, the renderer pulls data via HTTP
        # We push chunks to the stream session
        if self._stream_id:
            session = self._streamer.get_session(self._stream_id)
            if session:
                await session.put(data)

    async def flush(self) -> None:
        pass

    async def _dmr_call(self, method: str, *args, **kwargs) -> bool:
        """Call DMR method with timeout."""
        func = getattr(self._device, method)
        try:
            await asyncio.wait_for(func(*args, **kwargs), timeout=10)
            self._available = True
            return True
        except asyncio.TimeoutError:
            logger.warning("dlna_timeout", method=method, device=self.name)
            return False
        except Exception:
            logger.warning("dlna_call_error", method=method, device=self.name)
            return False

    async def pause(self) -> None:
        await self._dmr_call("async_pause")

    async def resume(self) -> None:
        await self._dmr_call("async_play")

    async def stop(self) -> None:
        await self._dmr_call("async_stop")
        if self._direct_url:
            self._direct_url = False
        elif self._stream_id:
            self._streamer.remove_session(self._stream_id)
            self._stream_id = None

    async def set_volume(self, volume: float) -> None:
        self._volume = volume
        await self._dmr_call("async_set_volume_level", volume)

    async def close(self) -> None:
        await self.stop()

    async def set_next_track(self, stream_info: AudioStreamInfo, track: Track) -> bool:
        """Use SetNextAVTransportURI for gapless playback."""
        try:
            # Direct URL for next track too if applicable
            if self.supports_direct_url(track):
                mime = mime_type_for_format(AudioFormat(track.format))
                metadata = _build_didl_lite(track, track.file_path, mime)
                await self._device.async_set_next_transport_uri(track.file_path, track.title or "Unknown", meta_data=metadata)
                logger.info("dlna_next_track_set_direct", track=track.title)
                return True

            stream_id = self._streamer.create_session(stream_info, track.file_path)
            stream_url = self._streamer.get_stream_url(stream_id, self._server_ip)
            mime = mime_type_for_format(stream_info.format)
            metadata = _build_didl_lite(track, stream_url, mime)

            await self._device.async_set_next_transport_uri(stream_url, track.title or "Unknown", meta_data=metadata)
            logger.info("dlna_next_track_set", track=track.title)
            return True
        except Exception:
            logger.debug("dlna_set_next_not_supported")
            return False

    def get_current_session(self):
        """Return the current stream session (for sync coordination)."""
        if self._stream_id:
            return self._streamer.get_session(self._stream_id)
        return None

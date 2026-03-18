from __future__ import annotations

import asyncio
import time
from xml.sax.saxutils import escape as xml_escape

import structlog

from tune_server.audio.formats import (
    DLNA_CAPABILITIES,
    AudioCapabilities,
    detect_dsd_from_device_info,
    detect_dsd_from_sink_protocols,
    dsd_mime_from_extension,
    mime_type_for_format,
)
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


def _build_didl_lite(
    track: Track, stream_url: str, mime_type: str,
    stream_info: AudioStreamInfo | None = None,
) -> str:
    """Build DIDL-Lite XML metadata for DLNA.

    When stream_info is provided (transcoded stream), use its audio properties
    instead of the source track's (e.g. DSD 2.8MHz/1-bit → WAV 192kHz/16-bit).
    """
    title = xml_escape(track.title or "Unknown")
    artist = xml_escape(track.artist_name or "Unknown Artist")
    album = xml_escape(track.album_title or "Unknown Album")

    # Use stream properties when transcoding, source properties when passthrough
    sample_rate = stream_info.sample_rate if stream_info else track.sample_rate
    bit_depth = stream_info.bit_depth if stream_info else track.bit_depth
    channels = stream_info.channels if stream_info else track.channels

    # Build res attributes
    res_attrs = f'protocolInfo="http-get:*:{mime_type}:*"'
    duration = _format_duration(track.duration_ms)
    if duration:
        res_attrs += f' duration="{duration}"'
    if sample_rate:
        res_attrs += f' sampleFrequency="{sample_rate}"'
    if bit_depth:
        res_attrs += f' bitsPerSample="{bit_depth}"'
    if channels:
        res_attrs += f' nrAudioChannels="{channels}"'

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
        sink_protocols: list[str] | None = None,
        device_name: str = "",
        device_model: str = "",
        device_ip: str | None = None,
    ) -> None:
        self._device = device
        self._streamer = streamer
        self._server_ip = server_ip
        self._stream_id: str | None = None
        self._direct_url: bool = False
        self._available = True
        self._volume: float = 0.5
        self._device_ip = device_ip
        # Micromega M-One: proprietary volume via HTTP on port 7000
        self._is_micromega = "micromega" in device_name.lower()
        if self._is_micromega:
            logger.info("micromega_device_detected", device=device_name, ip=device_ip)
        # DSD detection: protocol info first, then device name/model heuristic
        self._supports_native_dsd = (
            detect_dsd_from_sink_protocols(sink_protocols or [])
            or detect_dsd_from_device_info(device_name, device_model)
        )
        self._capabilities = self._build_capabilities()
        if self._supports_native_dsd:
            logger.info("dlna_dsd_support_detected", device=self.name)

    def _build_capabilities(self) -> AudioCapabilities:
        formats = {AudioFormat.FLAC, AudioFormat.WAV, AudioFormat.MP3, AudioFormat.AAC}
        if self._supports_native_dsd:
            formats.add(AudioFormat.DSD)
        return AudioCapabilities(
            formats=formats,
            max_sample_rate=192000,
            max_bit_depth=24,
            supports_gapless=True,
        )

    @property
    def name(self) -> str:
        return getattr(self._device, "name", "DLNA Renderer")

    @property
    def supports_native_dsd(self) -> bool:
        return self._supports_native_dsd

    @property
    def capabilities(self) -> AudioCapabilities:
        return self._capabilities

    @property
    def is_available(self) -> bool:
        return self._available

    def supports_direct_url(self, track: Track) -> bool:
        if not track or not track.file_path:
            return False
        if not (track.file_path.startswith("http://") or track.file_path.startswith("https://")):
            return False
        # Micromega: HTTPS streams (Tidal, Qobuz) are handled via the HTTP proxy in start().
        # Radio and streaming are both direct — no pipeline needed.
        if self._is_micromega:
            return True
        fmt = AudioFormat(track.format) if track.format else None
        return fmt in _DLNA_DIRECT_FORMATS

    async def start(self, stream_info: AudioStreamInfo, track: Track | None = None) -> None:
        self._direct_url = False

        try:
            # Direct URL passthrough: let the DLNA renderer fetch from the CDN
            if track and self.supports_direct_url(track):
                url = track.file_path
                # Micromega M-One doesn't support HTTPS — downgrade to HTTP
                if self._is_micromega and url.startswith("https://"):
                    url = "http://" + url[len("https://"):]
                    logger.info("micromega_https_downgrade", url=url[:80])

                mime = mime_type_for_format(AudioFormat(track.format))
                metadata = _build_didl_lite(track, url, mime)

                dmr = self._device
                title = track.title or "Unknown"
                await asyncio.wait_for(
                    dmr.async_set_transport_uri(url, title, meta_data=metadata), timeout=10
                )
                await asyncio.wait_for(dmr.async_play(), timeout=10)

                self._direct_url = True
                self._available = True
                logger.info("dlna_direct_url_playback", device=self.name, url=url[:80])
                return

            # Micromega proxy: relay HTTPS streaming URLs over HTTP with Content-Length
            if (
                self._is_micromega
                and track
                and track.file_path
                and track.file_path.startswith("https://")
                and track.source != Source.RADIO
            ):
                fmt = AudioFormat(track.format) if track.format else AudioFormat.FLAC
                mime = mime_type_for_format(fmt)
                proxy_info = AudioStreamInfo(
                    format=fmt,
                    sample_rate=track.sample_rate or 44100,
                    bit_depth=track.bit_depth or 16,
                    channels=track.channels or 2,
                )
                self._stream_id = self._streamer.create_proxy_session(track.file_path, proxy_info)
                stream_url = self._streamer.get_stream_url(self._stream_id, self._server_ip)
                metadata = _build_didl_lite(track, stream_url, mime)

                dmr = self._device
                title = track.title or "Unknown"
                await asyncio.wait_for(
                    dmr.async_set_transport_uri(stream_url, title, meta_data=metadata), timeout=10
                )
                await asyncio.wait_for(dmr.async_play(), timeout=10)

                self._direct_url = True
                self._available = True
                logger.info("micromega_proxy_playback", device=self.name, url=track.file_path[:80])
                return

            # Native DSD passthrough: serve DSF/DFF file directly to the renderer
            if (
                track
                and stream_info.format == AudioFormat.DSD
                and self._supports_native_dsd
                and track.file_path
                and not track.file_path.startswith("http")
            ):
                mime = dsd_mime_from_extension(track.file_path)
                self._stream_id = self._streamer.create_session(stream_info, track.file_path)
                stream_url = self._streamer.get_stream_url(self._stream_id, self._server_ip)
                metadata = _build_didl_lite(track, stream_url, mime)

                dmr = self._device
                title = track.title or "Unknown"
                await asyncio.wait_for(
                    dmr.async_set_transport_uri(stream_url, title, meta_data=metadata), timeout=10
                )
                await asyncio.wait_for(dmr.async_play(), timeout=10)

                self._available = True
                logger.info(
                    "dlna_native_dsd_playback", device=self.name,
                    file=track.file_path, mime=mime,
                    sample_rate=track.sample_rate,
                )
                return

            # Standard flow: stream via local HTTP server
            file_path = track.file_path if track else None
            self._stream_id = self._streamer.create_session(stream_info, file_path)
            stream_url = self._streamer.get_stream_url(self._stream_id, self._server_ip)

            mime = mime_type_for_format(stream_info.format)
            metadata = _build_didl_lite(track, stream_url, mime, stream_info=stream_info) if track else ""

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
        if self._is_micromega and self._device_ip:
            await self._micromega_set_volume(volume)
        else:
            await self._dmr_call("async_set_volume_level", volume)

    async def _micromega_set_volume(self, volume: float) -> None:
        """Set volume on Micromega M-One via proprietary HTTP protocol on port 7000.

        The M-One expects: GET /volume HTTP/1.0\\r\\n\\r\\nvolume=<value>\\r\\n
        where value is a float (0.0 to 100.0, matching the amplifier's display).
        Tune's 0.0-1.0 range maps to 0.0-100.0 on the M-One.
        """
        import socket

        target_vol = volume * 100.0
        msg = f"GET /volume HTTP/1.0\r\n\r\nvolume={target_vol:.1f}\r\n"

        def _send() -> None:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3)
                s.connect((self._device_ip, 7000))
                s.send(msg.encode())
                s.shutdown(socket.SHUT_WR)
                s.recv(256)  # read response
                s.close()
            except Exception:
                logger.debug("micromega_volume_error", device=self.name, volume=target_vol)

        await asyncio.to_thread(_send)
        logger.debug("micromega_volume_set", device=self.name, volume=target_vol)

    async def close(self) -> None:
        await self.stop()

    async def set_next_track(self, stream_info: AudioStreamInfo, track: Track) -> bool:
        """Use SetNextAVTransportURI for gapless playback."""
        try:
            # Direct URL for next track too if applicable
            if self.supports_direct_url(track):
                url = track.file_path
                if self._is_micromega and url.startswith("https://"):
                    url = "http://" + url[len("https://"):]
                mime = mime_type_for_format(AudioFormat(track.format))
                metadata = _build_didl_lite(track, url, mime)
                await self._device.async_set_next_transport_uri(url, track.title or "Unknown", meta_data=metadata)
                logger.info("dlna_next_track_set_direct", track=track.title)
                return True

            # Native DSD passthrough for next track
            if (
                stream_info.format == AudioFormat.DSD
                and self._supports_native_dsd
                and track.file_path
                and not track.file_path.startswith("http")
            ):
                mime = dsd_mime_from_extension(track.file_path)
                stream_id = self._streamer.create_session(stream_info, track.file_path)
                stream_url = self._streamer.get_stream_url(stream_id, self._server_ip)
                metadata = _build_didl_lite(track, stream_url, mime)
                await self._device.async_set_next_transport_uri(stream_url, track.title or "Unknown", meta_data=metadata)
                logger.info("dlna_next_track_set_native_dsd", track=track.title)
                return True

            stream_id = self._streamer.create_session(stream_info, track.file_path)
            stream_url = self._streamer.get_stream_url(stream_id, self._server_ip)
            mime = mime_type_for_format(stream_info.format)
            metadata = _build_didl_lite(track, stream_url, mime, stream_info=stream_info)

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

    async def get_position_ms(self) -> int:
        """Query the renderer's current playback position via GetPositionInfo."""
        try:
            dmr = self._device
            await asyncio.wait_for(dmr.async_update(do_ping=False), timeout=5)
            pos = dmr.media_position
            if pos is not None and pos >= 0:
                return int(pos * 1000)
        except asyncio.TimeoutError:
            logger.debug("dlna_position_timeout", device=self.name)
        except Exception:
            logger.debug("dlna_position_error", device=self.name)
        return -1

    async def measure_latency(self) -> float | None:
        """Measure actual DLNA startup latency by polling GetPositionInfo after start().

        Returns the time in seconds from start() to first media_position > 0,
        or None if timeout (10s).
        """
        start = time.monotonic()
        deadline = start + 10.0
        dmr = self._device
        while time.monotonic() < deadline:
            try:
                await asyncio.wait_for(dmr.async_update(do_ping=False), timeout=2)
                pos = dmr.media_position
                if pos is not None and pos > 0:
                    latency = time.monotonic() - start
                    logger.info("dlna_latency_measured", device=self.name, latency_s=round(latency, 2))
                    return latency
            except Exception:
                pass
            await asyncio.sleep(0.2)
        logger.warning("dlna_latency_timeout", device=self.name)
        return None

from __future__ import annotations

import asyncio
import time
from xml.sax.saxutils import escape as xml_escape

import aiohttp
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

    # Album art — convertit les chemins locaux en URL HTTP accessibles par le renderer
    art_tag = ""
    if track.cover_path:
        cover = track.cover_path
        if not cover.startswith("http"):
            from tune_server.config import settings
            from tune_server.utils.network import get_local_ip
            filename = cover.rsplit("/", 1)[-1] if "/" in cover else cover
            ip = get_local_ip() or "localhost"
            cover = f"http://{ip}:{settings.api_port}/api/v1/library/artwork/{filename}"
        art_tag = f'<upnp:albumArtURI>{xml_escape(cover)}</upnp:albumArtURI>'

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
        self._last_uri: str | None = None
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
        self._watchdog_task: asyncio.Task | None = None
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

    @property
    def is_direct_url(self) -> bool:
        return self._direct_url

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
        # Stop current playback before setting a new URI — many renderers
        # (Micromega, Wiim, Denon) ignore a new URI without explicit Stop.
        from tune_server.config import settings as _s
        _start_t = time.monotonic()
        _had_active_stream = self._stream_id is not None or self._last_uri is not None
        try:
            await self._dmr_call("async_stop")
            # Only wait settle delay if we were actually playing something —
            # skip for first track to reduce startup latency
            if _had_active_stream and _s.dlna_settle_ms > 0:
                await asyncio.sleep(_s.dlna_settle_ms / 1000.0)
        except Exception:
            logger.debug("dlna_pre_start_stop_failed", device=self.name)
        self._cancel_watchdog()
        if self._stream_id:
            self._streamer.remove_session(self._stream_id)
            self._stream_id = None
        self._direct_url = False

        try:
            # Radio proxy: buffer the infinite stream locally so the renderer
            # fetches from LAN (absorbs CDN blips / network glitches)
            if (
                track
                and track.source == Source.RADIO
                and track.file_path
                and track.file_path.startswith("http")
            ):
                fmt = AudioFormat(track.format) if track.format else AudioFormat.AAC
                mime = mime_type_for_format(fmt)
                proxy_info = AudioStreamInfo(
                    format=fmt,
                    sample_rate=track.sample_rate or 44100,
                    bit_depth=track.bit_depth or 16,
                    channels=track.channels or 2,
                )
                self._stream_id = self._streamer.create_radio_proxy_session(track.file_path, proxy_info)
                stream_url = self._streamer.get_stream_url(self._stream_id, self._server_ip)
                metadata = _build_didl_lite(track, stream_url, mime)

                dmr = self._device
                title = track.title or "Unknown"
                await self._set_and_play(dmr, stream_url, title, metadata)
                self._start_watchdog(stream_url, title, metadata)

                self._direct_url = True
                self._last_uri = stream_url
                self._available = True
                _elapsed = round((time.monotonic() - _start_t) * 1000)
                logger.info("radio_proxy_playback", device=self.name, url=track.file_path[:80], startup_ms=_elapsed)
                return

            # Direct URL passthrough: let the DLNA renderer fetch from the CDN
            if track and self.supports_direct_url(track):
                url = track.file_path
                # Micromega M-One: proxy all external URLs (can't handle HTTPS or redirects)
                if self._is_micromega and url.startswith("http"):
                    # Fall through to Micromega proxy below
                    pass
                else:
                    mime = mime_type_for_format(AudioFormat(track.format))
                    metadata = _build_didl_lite(track, url, mime)

                    dmr = self._device
                    title = track.title or "Unknown"
                    await self._set_and_play(dmr, url, title, metadata)

                    self._direct_url = True
                    self._last_uri = url
                    self._available = True
                    _elapsed = round((time.monotonic() - _start_t) * 1000)
                    logger.info("dlna_direct_url_playback", device=self.name, url=url[:80], startup_ms=_elapsed)
                    return

            # Micromega proxy: relay external URLs over HTTP
            # (Micromega can't handle HTTPS or follow redirects)
            if (
                self._is_micromega
                and track
                and track.file_path
                and (track.file_path.startswith("https://") or track.file_path.startswith("http://"))
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
                await self._set_and_play(dmr, stream_url, title, metadata)
                self._start_watchdog(stream_url, title, metadata)

                self._direct_url = True
                self._last_uri = stream_url
                self._available = True
                _elapsed = round((time.monotonic() - _start_t) * 1000)
                logger.info("micromega_proxy_playback", device=self.name, url=track.file_path[:80], startup_ms=_elapsed)
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
                # Replace generic .dsd extension with actual file extension (.dsf/.dff)
                # for better renderer compatibility
                dsd_ext = "dff" if track.file_path.lower().endswith(".dff") else "dsf"
                stream_url = stream_url.rsplit(".", 1)[0] + f".{dsd_ext}"
                metadata = _build_didl_lite(track, stream_url, mime)

                dmr = self._device
                title = track.title or "Unknown"
                await self._set_and_play(dmr, stream_url, title, metadata)
                self._start_watchdog(stream_url, title, metadata)

                self._direct_url = True
                self._last_uri = stream_url
                self._available = True
                _elapsed = round((time.monotonic() - _start_t) * 1000)
                logger.info(
                    "dlna_native_dsd_playback", device=self.name,
                    file=track.file_path, mime=mime,
                    sample_rate=track.sample_rate,
                    startup_ms=_elapsed,
                )
                return

            # Standard flow: stream via local HTTP server
            file_path = track.file_path if track else None
            self._stream_id = self._streamer.create_session(stream_info, file_path)
            stream_url = self._streamer.get_stream_url(self._stream_id, self._server_ip)

            # If the streamer will serve the file directly (passthrough with file on disk),
            # mark as direct_url so the pipeline's write() calls are no-ops.
            if file_path and stream_info.file_size and not file_path.startswith("http"):
                self._direct_url = True
                # Use track's native format for MIME (not pipeline's output format)
                mime = mime_type_for_format(AudioFormat(track.format)) if track and track.format else mime_type_for_format(stream_info.format)
            else:
                mime = mime_type_for_format(stream_info.format)
            metadata = _build_didl_lite(track, stream_url, mime, stream_info=stream_info) if track else ""

            title = track.title if track else "Unknown"
            dmr = self._device
            await self._set_and_play(dmr, stream_url, title, metadata)
            self._start_watchdog(stream_url, title, metadata)

            self._last_uri = stream_url
            self._available = True
            _elapsed = round((time.monotonic() - _start_t) * 1000)
            logger.info("dlna_playback_started", device=self.name, url=stream_url, startup_ms=_elapsed)
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

    async def _set_and_play(self, dmr, stream_url: str, title: str, metadata: str) -> None:
        from tune_server.config import settings as _s
        t0 = time.monotonic()
        await asyncio.wait_for(
            dmr.async_set_transport_uri(stream_url, title, meta_data=metadata), timeout=10
        )
        t_uri = time.monotonic()
        if _s.dlna_play_delay_ms > 0:
            await asyncio.sleep(_s.dlna_play_delay_ms / 1000.0)
        await asyncio.wait_for(dmr.async_play(), timeout=10)
        t_play = time.monotonic()
        logger.info(
            "dlna_set_and_play_timing",
            device=self.name,
            set_uri_ms=round((t_uri - t0) * 1000),
            play_cmd_ms=round((t_play - t_uri) * 1000),
            total_ms=round((t_play - t0) * 1000),
        )

    def _start_watchdog(self, stream_url: str, title: str, metadata: str) -> None:
        """Start a watchdog that retries SetAVTransportURI if the renderer
        doesn't fetch the HTTP stream within 30 seconds."""
        self._cancel_watchdog()
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(stream_url, title, metadata)
        )

    def _cancel_watchdog(self) -> None:
        """Cancel any pending watchdog timer."""
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            self._watchdog_task = None

    async def _watchdog_loop(self, stream_url: str, title: str, metadata: str) -> None:
        """Wait up to 30s for the renderer to connect. If it doesn't, retry
        SetAVTransportURI once. If the second attempt also times out, log
        an error and give up."""
        try:
            session = self._streamer.get_session(self._stream_id) if self._stream_id else None
            if not session:
                return

            # Wait for the renderer to make its first HTTP GET
            try:
                await asyncio.wait_for(session.client_connected.wait(), timeout=30)
                logger.debug("dlna_watchdog_ok", device=self.name, msg="renderer connected")
                return  # success — renderer fetched the stream
            except asyncio.TimeoutError:
                pass

            # Renderer didn't connect within 30s — retry SetAVTransportURI
            logger.warning(
                "dlna_watchdog_retry",
                device=self.name,
                msg="renderer did not fetch stream within 30s, retrying SetAVTransportURI",
            )
            try:
                dmr = self._device
                await asyncio.wait_for(dmr.async_stop(), timeout=5)
                await asyncio.sleep(0.5)
                await self._set_and_play(dmr, stream_url, title, metadata)
            except Exception:
                logger.exception("dlna_watchdog_retry_failed", device=self.name)
                return

            # Wait another 30s after retry
            try:
                await asyncio.wait_for(session.client_connected.wait(), timeout=30)
                logger.info("dlna_watchdog_retry_ok", device=self.name,
                            msg="renderer connected after retry")
            except asyncio.TimeoutError:
                logger.error("dlna_watchdog_gave_up", device=self.name,
                             msg="renderer still not fetching after retry")

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("dlna_watchdog_error", device=self.name, exc_info=True)

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
        ok = await self._dmr_call("async_play")
        if not ok:
            logger.warning("dlna_resume_failed_retry", device=self.name)
            # Some renderers need SetAVTransportURI again after pause
            if self._last_uri:
                await self._dmr_call("async_set_transport_uri", self._last_uri)
                await self._dmr_call("async_play")

    async def stop(self) -> None:
        self._cancel_watchdog()
        await self._dmr_call("async_stop")
        if self._direct_url:
            self._direct_url = False
        if self._stream_id:
            self._streamer.remove_session(self._stream_id)
            self._stream_id = None

    async def seek(self, position_ms: int) -> bool:
        """Native DLNA seek via Seek(REL_TIME). Works when renderer supports it."""
        dmr = self._device
        if not getattr(dmr, "can_seek_rel_time", False):
            return False
        total_s, ms = divmod(position_ms, 1000)
        hours, rem = divmod(total_s, 3600)
        minutes, seconds = divmod(rem, 60)
        target = f"{hours}:{minutes:02d}:{seconds:02d}"
        try:
            await asyncio.wait_for(dmr.async_seek_rel_time(target), timeout=5)
            logger.info("dlna_native_seek", device=self.name, position=target)
            return True
        except Exception:
            logger.debug("dlna_native_seek_failed", device=self.name)
            return False

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
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.settimeout(3)
                s.connect((self._device_ip, 7000))
                s.send(msg.encode())
                s.shutdown(socket.SHUT_WR)
                s.recv(256)  # read response
            except Exception:
                logger.debug("micromega_volume_error", device=self.name, volume=target_vol)
            finally:
                s.close()

        await asyncio.to_thread(_send)
        logger.debug("micromega_volume_set", device=self.name, volume=target_vol)

    async def _resolve_redirects(self, url: str, max_hops: int = 5) -> str:
        """Follow redirects manually, forcing HTTP at each hop."""
        try:
            current = url
            async with aiohttp.ClientSession() as session:
                for _ in range(max_hops):
                    async with session.head(current, allow_redirects=False,
                                            timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status not in (301, 302, 303, 307, 308):
                            break  # Final destination
                        location = resp.headers.get("Location", "")
                        if not location:
                            break
                        # Force HTTP
                        if location.startswith("https://"):
                            location = "http://" + location[len("https://"):]
                        current = location
            if current != url:
                logger.info("dlna_redirect_resolved", original=url[:60], final=current[:80])
            return current
        except Exception as e:
            logger.debug("dlna_redirect_resolve_failed", url=url[:60], error=str(e))
            return url

    async def close(self) -> None:
        self._cancel_watchdog()
        await self.stop()

    async def set_next_track(self, stream_info: AudioStreamInfo, track: Track,
                             gapless_handler=None) -> bool:
        """Use SetNextAVTransportURI for gapless playback.

        Creates an HTTP streamer session for the next track so the renderer
        can fetch it seamlessly when the current track ends.  Works for
        direct URLs, native DSD, local file passthrough, and transcoded streams.
        """
        try:
            # Direct URL for next track too if applicable
            if self.supports_direct_url(track):
                url = track.file_path
                if self._is_micromega:
                    if url.startswith("https://"):
                        url = "http://" + url[len("https://"):]
                    url = await self._resolve_redirects(url)
                mime = mime_type_for_format(AudioFormat(track.format))
                metadata = _build_didl_lite(track, url, mime)
                await self._device.async_set_next_transport_uri(url, track.title or "Unknown", meta_data=metadata)
                logger.info("dlna_next_track_set_direct", device=self.name, track=track.title)
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
                dsd_ext = "dff" if track.file_path.lower().endswith(".dff") else "dsf"
                stream_url = stream_url.rsplit(".", 1)[0] + f".{dsd_ext}"
                metadata = _build_didl_lite(track, stream_url, mime)
                await self._device.async_set_next_transport_uri(stream_url, track.title or "Unknown", meta_data=metadata)
                logger.info("dlna_next_track_set_native_dsd", device=self.name, track=track.title)
                return True

            # Transcoded gapless: preloader provides WAV data via streaming session
            if gapless_handler is not None:
                mime = mime_type_for_format(stream_info.format)
                stream_id = self._streamer.create_session(stream_info)
                stream_url = self._streamer.get_stream_url(stream_id, self._server_ip)
                metadata = _build_didl_lite(track, stream_url, mime, stream_info=stream_info)

                await self._device.async_set_next_transport_uri(
                    stream_url, track.title or "Unknown", meta_data=metadata)

                asyncio.create_task(
                    self._feed_gapless_session(stream_id, gapless_handler))

                logger.info("dlna_next_track_set_transcoded", device=self.name,
                            track=track.title, stream_url=stream_url,
                            format=stream_info.format.value)
                return True

            # Passthrough: serve local file directly
            is_local_file = track.file_path and not track.file_path.startswith("http")
            mime = mime_type_for_format(stream_info.format)
            stream_id = self._streamer.create_session(stream_info, track.file_path)
            stream_url = self._streamer.get_stream_url(stream_id, self._server_ip)
            metadata = _build_didl_lite(track, stream_url, mime, stream_info=stream_info)

            await self._device.async_set_next_transport_uri(stream_url, track.title or "Unknown", meta_data=metadata)
            logger.info(
                "dlna_next_track_set", device=self.name, track=track.title,
                stream_url=stream_url, local_file=is_local_file,
            )
            return True
        except Exception:
            logger.exception("dlna_set_next_failed", device=self.name, track=track.title)
            return False

    async def _feed_gapless_session(self, stream_id: str, gapless_handler) -> None:
        """Feed transcoded audio from gapless preloader into a streaming session."""
        session = self._streamer.get_session(stream_id)
        if not session:
            return
        try:
            # Push pre-buffered chunks first
            for chunk in gapless_handler.pre_buffered_chunks:
                await session.put(chunk)

            # Continue reading from the preloader's pipeline
            pipeline = gapless_handler._next_pipeline
            if pipeline and pipeline.output_buffer:
                while session.active:
                    try:
                        chunk = await asyncio.wait_for(
                            pipeline.output_buffer.get(), timeout=5.0)
                        if chunk is None:
                            break
                        await session.put(chunk)
                    except asyncio.TimeoutError:
                        break

            session.close()
            logger.info("gapless_feed_completed", stream_id=stream_id)
        except Exception:
            logger.exception("gapless_feed_error", stream_id=stream_id)

    def get_current_session(self):
        """Return the current stream session (for sync coordination)."""
        if self._stream_id:
            return self._streamer.get_session(self._stream_id)
        return None

    async def get_position_ms(self) -> int:
        """Query the renderer's current playback position via GetPositionInfo.

        Returns -2 if the renderer has stopped (track finished).
        Returns -1 if position is unknown.
        """
        try:
            dmr = self._device
            await asyncio.wait_for(dmr.async_update(do_ping=False), timeout=5)

            # Check if renderer has stopped playing
            transport_state = getattr(dmr, "transport_state", None)
            if transport_state in ("STOPPED", "NO_MEDIA_PRESENT"):
                return -2  # signal track end

            pos = dmr.media_position
            if pos is not None and pos >= 0:
                return int(pos * 1000)
        except asyncio.TimeoutError:
            logger.debug("dlna_position_timeout", device=self.name)
        except Exception:
            logger.debug("dlna_position_error", device=self.name)
        return -1

    def get_current_track_uri(self) -> str | None:
        """Return the URI the renderer is currently playing (from last async_update).

        Works with both async_upnp_client DmrDevice (current_track_uri property)
        and MinimalDmrDevice (_current_track_uri attribute).
        """
        return getattr(self._device, "current_track_uri", None)

    def has_uri_changed(self) -> bool:
        """Check if the renderer is now playing a different URI than what we set.

        Returns True when the renderer has auto-advanced to the next track
        (gapless transition via SetNextAVTransportURI).  Called after
        get_position_ms() which triggers async_update().
        """
        renderer_uri = self.get_current_track_uri()
        if not renderer_uri or not self._last_uri:
            return False
        return renderer_uri != self._last_uri

    def sync_last_uri(self) -> None:
        """Update _last_uri to the renderer's current URI.

        Called after a gapless transition is detected so subsequent
        has_uri_changed() calls compare against the new track's URI.
        """
        renderer_uri = self.get_current_track_uri()
        if renderer_uri:
            self._last_uri = renderer_uri

    async def get_volume(self) -> float | None:
        """Read the current volume level from the renderer (0.0-1.0).

        Returns None if volume cannot be read (renderer doesn't support it).
        """
        if self._is_micromega:
            # Micromega uses proprietary protocol, just return cached value
            return self._volume
        try:
            dmr = self._device
            vol = getattr(dmr, "volume_level", None)
            if vol is not None:
                return float(vol)
        except Exception:
            logger.debug("dlna_get_volume_error", device=self.name)
        return None

    async def fade_volume(
        self,
        from_vol: float,
        to_vol: float,
        duration: float,
        steps: int = 20,
    ) -> bool:
        """Ramp DLNA volume from from_vol to to_vol over duration seconds.

        Both from_vol and to_vol are in 0.0-1.0 range (DLNA standard).
        Returns True if the fade completed, False if volume control failed
        on the first attempt (renderer doesn't support SetVolume).

        The fade is best-effort: individual step failures are silently skipped
        so playback is never interrupted.
        """
        if steps < 1 or duration <= 0:
            return False

        step_delay = duration / steps
        vol_delta = (to_vol - from_vol) / steps

        for i in range(steps + 1):
            target = from_vol + vol_delta * i
            target = max(0.0, min(1.0, target))
            try:
                if self._is_micromega and self._device_ip:
                    await self._micromega_set_volume(target)
                else:
                    ok = await self._dmr_call("async_set_volume_level", target)
                    if not ok and i == 0:
                        # First step failed — renderer doesn't support volume control
                        logger.debug("dlna_fade_volume_not_supported", device=self.name)
                        return False
            except Exception:
                if i == 0:
                    logger.debug("dlna_fade_volume_not_supported", device=self.name)
                    return False
                # Later steps: skip silently
            self._volume = target
            if i < steps:
                await asyncio.sleep(step_delay)

        logger.info(
            "dlna_fade_volume_done",
            device=self.name,
            from_vol=round(from_vol, 2),
            to_vol=round(to_vol, 2),
            duration=round(duration, 1),
        )
        return True

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
            except Exception as e:
                logger.debug("dlna_latency_poll_error", device=self.name, error=str(e))
            await asyncio.sleep(0.2)
        logger.warning("dlna_latency_timeout", device=self.name)
        return None

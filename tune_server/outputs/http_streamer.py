from __future__ import annotations

import asyncio
import struct
import time
import uuid
from typing import Optional

import structlog
import aiohttp
from aiohttp import web

from tune_server.audio.formats import dsd_mime_from_extension, mime_type_for_format
from tune_server.config import settings
from tune_server.models import AudioFormat, AudioStreamInfo

logger = structlog.get_logger()


try:
    from tune_native import build_wav_header as _rust_wav_header
    def _build_wav_header(stream_info: AudioStreamInfo) -> bytes:
        channels = stream_info.channels or 2
        sample_rate = stream_info.sample_rate or 44100
        bit_depth = stream_info.bit_depth or 16
        return _rust_wav_header(channels, sample_rate, bit_depth)
except ImportError:
    def _build_wav_header(stream_info: AudioStreamInfo) -> bytes:
        channels = stream_info.channels or 2
        sample_rate = stream_info.sample_rate or 44100
        bit_depth = stream_info.bit_depth or 16
        byte_rate = sample_rate * channels * (bit_depth // 8)
        block_align = channels * (bit_depth // 8)
        data_size = 0x7FFFFFFF
        file_size = data_size + 36
        return struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", file_size, b"WAVE", b"fmt ", 16, 1,
            channels, sample_rate, byte_rate, block_align, bit_depth,
            b"data", data_size,
        )


class StreamSession:
    """A single audio stream session."""

    def __init__(self, stream_id: str, stream_info: AudioStreamInfo, bit_perfect: bool = False, max_chunks: int = 256) -> None:
        self.stream_id = stream_id
        self.stream_info = stream_info
        self.bit_perfect = bit_perfect
        self._chunks: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=max_chunks)
        self.active = True
        self.file_served = False  # True when _serve_file handles the request directly
        self.client_connected = asyncio.Event()  # set when renderer makes first HTTP request
        self.created_at = time.monotonic()
        self.last_activity = time.monotonic()
        # Active HTTP responses serving this session — forcibly closed on stop
        self._active_responses: list[web.StreamResponse] = []
        # Track metadata — used for ICY headers (BluOS, etc.)
        self.track_title: str | None = None
        self.track_artist: str | None = None
        self.track_album: str | None = None
        self.track_cover_url: str | None = None

    def register_response(self, response: web.StreamResponse) -> None:
        """Track an active HTTP response so it can be aborted on stop."""
        self._active_responses.append(response)

    def unregister_response(self, response: web.StreamResponse) -> None:
        """Remove a response from tracking (normal completion)."""
        try:
            self._active_responses.remove(response)
        except ValueError:
            pass

    async def put(self, data: bytes) -> None:
        if self.active and not self.file_served:
            self.last_activity = time.monotonic()
            await self._chunks.put(data)

    async def get(self) -> Optional[bytes]:
        return await self._chunks.get()

    def close(self) -> None:
        self.active = False
        try:
            self._chunks.put_nowait(None)
        except asyncio.QueueFull:
            logger.debug("stream_session_queue_full")
        # Abort all active HTTP responses to force the renderer to stop
        # receiving data (prevents 20-30s buffer lag on Stop/Next)
        for resp in self._active_responses:
            try:
                writer = resp._payload_writer  # type: ignore[attr-defined]
                if writer is not None:
                    transport = getattr(writer, "transport", None)
                    if transport is not None and not transport.is_closing():
                        transport.close()
            except Exception:
                pass
        self._active_responses.clear()


class HttpAudioStreamer:
    """HTTP server that serves audio to DLNA renderers.

    Runs on a separate port (default 8080) and serves audio streams
    with proper DLNA headers and Range request support.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        self._host = host
        self._port = port
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._route_callbacks: list = []
        self._sessions: dict[str, StreamSession] = {}
        self._file_paths: dict[str, str] = {}  # stream_id -> file_path for passthrough
        self._proxy_urls: dict[str, str] = {}  # stream_id -> upstream HTTPS URL for proxy
        self._radio_sessions: set[str] = set()  # stream_ids for infinite radio streams
        self._cleanup_task: asyncio.Task | None = None
        self._http_session: aiohttp.ClientSession | None = None

    @property
    def app(self) -> web.Application | None:
        return self._app

    def on_app_created(self, callback) -> None:
        """Register a callback to add routes after app creation, before freeze."""
        self._route_callbacks.append(callback)

    @property
    def port(self) -> int:
        return self._port

    def create_session(self, stream_info: AudioStreamInfo, file_path: str | None = None, bit_perfect: bool = False, max_chunks: int = 256) -> str:
        stream_id = str(uuid.uuid4())
        self._sessions[stream_id] = StreamSession(stream_id, stream_info, bit_perfect=bit_perfect, max_chunks=max_chunks)
        if file_path:
            self._file_paths[stream_id] = file_path
        logger.info("stream_session_created", stream_id=stream_id)
        return stream_id

    def get_session(self, stream_id: str) -> Optional[StreamSession]:
        return self._sessions.get(stream_id)

    def set_session_metadata(
        self,
        stream_id: str,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        cover_url: str | None = None,
    ) -> None:
        """Attach track metadata to a session for ICY headers (BluOS etc.)."""
        session = self._sessions.get(stream_id)
        if session:
            session.track_title = title
            session.track_artist = artist
            session.track_album = album
            session.track_cover_url = cover_url

    def create_proxy_session(self, upstream_url: str, stream_info: AudioStreamInfo, bit_perfect: bool = False) -> str:
        """Create a session that proxies an upstream HTTPS URL over HTTP."""
        stream_id = str(uuid.uuid4())
        self._sessions[stream_id] = StreamSession(stream_id, stream_info, bit_perfect=bit_perfect)
        self._proxy_urls[stream_id] = upstream_url
        logger.info("proxy_session_created", stream_id=stream_id, url=upstream_url[:80])
        return stream_id

    def create_radio_proxy_session(self, upstream_url: str, stream_info: AudioStreamInfo) -> str:
        """Create a proxy session for an infinite radio stream (no timeout)."""
        stream_id = self.create_proxy_session(upstream_url, stream_info)
        self._radio_sessions.add(stream_id)
        logger.info("radio_proxy_session_created", stream_id=stream_id, url=upstream_url[:80])
        return stream_id

    def remove_session(self, stream_id: str) -> None:
        session = self._sessions.pop(stream_id, None)
        if session:
            session.close()
        self._file_paths.pop(stream_id, None)
        self._proxy_urls.pop(stream_id, None)
        self._radio_sessions.discard(stream_id)

    def _resolve_mime(self, stream_id: str, session) -> str:
        """Return the correct MIME type, using file extension for DSD."""
        if session.stream_info.format == AudioFormat.DSD:
            file_path = self._file_paths.get(stream_id, "")
            return dsd_mime_from_extension(file_path)
        return mime_type_for_format(session.stream_info.format)

    def _tune_timing_headers(self, session: StreamSession) -> dict[str, str]:
        """Build precision timing headers from the stream session.
        Disabled: non-standard headers cause some DLNA renderers (DMP-A8)
        to drop the HTTP connection after ~11 seconds.
        """
        return {}

    def get_stream_url(self, stream_id: str, server_ip: str) -> str:
        session = self._sessions.get(stream_id)
        if not session:
            return ""
        ext = session.stream_info.format.value if hasattr(session.stream_info.format, 'value') else session.stream_info.format
        return f"http://{server_ip}:{self._port}/stream/{stream_id}.{ext}"

    def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def start(self) -> None:
        self._app = web.Application()
        self._app.router.add_route("HEAD", "/stream/{stream_id}", self._handle_head)
        self._app.router.add_route("GET", "/stream/{stream_id}", self._handle_stream)

        # Appel des callbacks pour ajouter des routes (UPnP, etc.) avant freeze
        for cb in self._route_callbacks:
            cb(self._app)

        self._runner = web.AppRunner(self._app, keepalive_timeout=30)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner, self._host, self._port,
            reuse_address=True,
        )
        await site.start()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("http_streamer_started", host=self._host, port=self._port)

    @staticmethod
    def _build_icy_metadata(session: StreamSession) -> bytes:
        """Build an ICY metadata block from session track metadata.

        ICY protocol: metadata is a length byte (size = byte_val * 16)
        followed by the ASCII payload padded with NULs to a 16-byte boundary.
        """
        parts: list[str] = []
        stream_title_parts: list[str] = []
        if session.track_artist:
            stream_title_parts.append(session.track_artist)
        if session.track_title:
            stream_title_parts.append(session.track_title)
        if stream_title_parts:
            parts.append(f"StreamTitle='{' - '.join(stream_title_parts)}';")
        if session.track_cover_url:
            parts.append(f"StreamUrl='{session.track_cover_url}';")
        if not parts:
            return b"\x00"
        payload = "".join(parts).encode("utf-8", errors="replace")
        # pad to 16-byte boundary
        pad_len = (16 - len(payload) % 16) % 16
        payload += b"\x00" * pad_len
        length_byte = len(payload) // 16
        if length_byte > 255:
            # truncate if absurdly long
            payload = payload[:255 * 16]
            length_byte = 255
        return bytes([length_byte]) + payload

    async def _handle_head(self, request: web.Request) -> web.Response:
        stream_id = request.match_info["stream_id"].split(".")[0]
        session = self._sessions.get(stream_id)

        if not session:
            return web.Response(status=404)

        mime = self._resolve_mime(stream_id, session)
        headers = {
            "Content-Type": mime,
            "Accept-Ranges": "bytes",
            "Connection": "keep-alive",
            "transferMode.dlna.org": "Streaming",
            "contentFeatures.dlna.org": "",
        }
        # Advertise ICY metadata capability so BluOS/network players
        # know to request it on the subsequent GET.
        if session.track_title or session.track_artist:
            icy_parts: list[str] = []
            if session.track_artist:
                icy_parts.append(session.track_artist)
            if session.track_title:
                icy_parts.append(session.track_title)
            headers["icy-name"] = " - ".join(icy_parts)
            if session.track_album:
                headers["icy-description"] = session.track_album

        # For local files, return Content-Length immediately from file_size
        file_path = self._file_paths.get(stream_id)
        if file_path and session.stream_info.file_size:
            headers["Content-Length"] = str(session.stream_info.file_size)
            return web.Response(headers=headers)

        # For proxy sessions, fetch Content-Length from upstream
        proxy_url = self._proxy_urls.get(stream_id)
        if proxy_url:
            try:
                cs = self._get_http_session()
                async with cs.head(proxy_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        cl = resp.headers.get("Content-Length")
                        if cl:
                            headers["Content-Length"] = cl
                        upstream_ct = resp.headers.get("Content-Type")
                        if upstream_ct:
                            headers["Content-Type"] = upstream_ct
            except Exception as e:
                logger.debug("stream_head_request_failed", error=str(e))
        elif session.stream_info.file_size:
            headers["Content-Length"] = str(session.stream_info.file_size)

        return web.Response(headers=headers)

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        stream_id = request.match_info["stream_id"].split(".")[0]
        session = self._sessions.get(stream_id)

        if not session:
            return web.Response(status=404)

        mime = self._resolve_mime(stream_id, session)

        # Signal that the renderer has connected and log connection latency
        session.client_connected.set()
        connect_latency = time.monotonic() - session.created_at
        logger.info(
            "stream_renderer_connected",
            stream_id=stream_id,
            connect_latency_ms=round(connect_latency * 1000),
        )

        # Check for file-based passthrough with Range support
        file_path = self._file_paths.get(stream_id)
        if file_path and session.stream_info.file_size:
            try:
                session.file_served = True
                return await self._serve_file(request, file_path, mime, session.stream_info.file_size, session)
            except Exception:
                logger.debug("stream_client_disconnected", stream_id=stream_id)
                return web.Response(status=499)  # client closed

        # Proxy mode: relay upstream HTTPS content over HTTP
        proxy_url = self._proxy_urls.get(stream_id)
        if proxy_url:
            try:
                return await self._proxy_stream(request, proxy_url, mime, stream_id, session)
            except Exception:
                logger.debug("proxy_client_disconnected", stream_id=stream_id)
                return web.Response(status=499)

        # Streaming mode
        #
        # ICY metadata: when the session carries track metadata AND the
        # client requests ICY (Icy-MetaData: 1), we interleave metadata
        # blocks every ``icy_metaint`` bytes.  This is the standard way
        # network players (BluOS, Squeeze, …) discover title/artist.
        has_icy_meta = session.track_title or session.track_artist
        client_wants_icy = request.headers.get("Icy-MetaData") == "1"
        icy_metaint = 16384  # bytes between metadata blocks

        stream_headers = {
            "Content-Type": mime,
            "transferMode.dlna.org": "Streaming",
            "Cache-Control": "no-cache",
        }
        # Always advertise ICY name/description when metadata is present
        # so that players aware of ICY can display something even without
        # inline metadata insertion.
        if has_icy_meta:
            icy_name_parts: list[str] = []
            if session.track_artist:
                icy_name_parts.append(session.track_artist)
            if session.track_title:
                icy_name_parts.append(session.track_title)
            stream_headers["icy-name"] = " - ".join(icy_name_parts)
            if session.track_album:
                stream_headers["icy-description"] = session.track_album

        if has_icy_meta and client_wants_icy:
            stream_headers["icy-metaint"] = str(icy_metaint)

        stream_headers.update(self._tune_timing_headers(session))
        response = web.StreamResponse(headers=stream_headers)
        await response.prepare(request)
        session.register_response(response)

        try:
            # Prepend WAV header when streaming decoded PCM as WAV
            if session.stream_info.format == AudioFormat.WAV:
                wav_header = _build_wav_header(session.stream_info)
                await response.write(wav_header)

            if has_icy_meta and client_wants_icy:
                # ICY interleaved mode: insert metadata every icy_metaint bytes
                icy_block = self._build_icy_metadata(session)
                bytes_since_meta = 0
                while session.active:
                    chunk = await session.get()
                    if chunk is None:
                        break
                    offset = 0
                    while offset < len(chunk):
                        remaining = icy_metaint - bytes_since_meta
                        end = min(offset + remaining, len(chunk))
                        await response.write(chunk[offset:end])
                        bytes_since_meta += end - offset
                        offset = end
                        if bytes_since_meta >= icy_metaint:
                            await response.write(icy_block)
                            bytes_since_meta = 0
            else:
                while session.active:
                    chunk = await session.get()
                    if chunk is None:
                        break
                    await response.write(chunk)
        except (ConnectionResetError, ConnectionAbortedError, Exception) as e:
            if "closing transport" in str(e).lower():
                logger.debug("stream_client_disconnected", stream_id=stream_id)
            else:
                logger.debug("stream_write_error", stream_id=stream_id, error=str(e))
        finally:
            session.unregister_response(response)
            try:
                await response.write_eof()
            except Exception as e:
                logger.debug("stream_write_eof_failed", stream_id=stream_id, error=str(e))

        return response

    async def _serve_file(
        self, request: web.Request, file_path: str, mime: str, file_size: int,
        session: StreamSession | None = None,
    ) -> web.StreamResponse:
        """Serve a file with Range request support for DLNA."""
        timing = self._tune_timing_headers(session) if session else {}
        # Include ICY name headers so network players (BluOS etc.) can
        # display metadata even when serving a complete file.
        if session and (session.track_title or session.track_artist):
            icy_parts: list[str] = []
            if session.track_artist:
                icy_parts.append(session.track_artist)
            if session.track_title:
                icy_parts.append(session.track_title)
            timing["icy-name"] = " - ".join(icy_parts)
            if session.track_album:
                timing["icy-description"] = session.track_album
        range_header = request.headers.get("Range")

        if range_header:
            try:
                range_spec = range_header.replace("bytes=", "")
                start_str, end_str = range_spec.split("-")
                start = int(start_str) if start_str else 0
                end = int(end_str) if end_str else file_size - 1
                length = end - start + 1

                range_headers = {
                    "Content-Type": mime,
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(length),
                    "Accept-Ranges": "bytes",
                    "Connection": "keep-alive",
                    "transferMode.dlna.org": "Streaming",
                }
                range_headers.update(timing)
                response = web.StreamResponse(
                    status=206,
                    headers=range_headers,
                )
                await response.prepare(request)
                if session:
                    session.register_response(response)

                try:
                    with open(file_path, "rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0 and (not session or session.active):
                            chunk_size = min(65536, remaining)
                            chunk = await asyncio.to_thread(f.read, chunk_size)
                            if not chunk:
                                break
                            await response.write(chunk)
                            remaining -= len(chunk)

                    if not session or session.active:
                        await response.write_eof()
                except Exception:
                    logger.debug("serve_file_client_disconnected", stream_id=request.match_info.get("stream_id"))
                finally:
                    if session:
                        session.unregister_response(response)
                return response
            except (ValueError, OSError) as e:
                logger.debug("serve_file_range_parse_failed", error=str(e))

        # Full file response
        full_headers = {
            "Content-Type": mime,
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes",
            "Connection": "keep-alive",
            "transferMode.dlna.org": "Streaming",
        }
        full_headers.update(timing)
        response = web.StreamResponse(headers=full_headers)
        await response.prepare(request)
        if session:
            session.register_response(response)

        try:
            with open(file_path, "rb") as f:
                while not session or session.active:
                    chunk = await asyncio.to_thread(f.read, 65536)
                    if not chunk:
                        break
                    await response.write(chunk)

            if not session or session.active:
                await response.write_eof()
        except Exception:
            logger.debug("serve_file_client_disconnected", stream_id=request.match_info.get("stream_id"))
        finally:
            if session:
                session.unregister_response(response)
        return response

    async def _proxy_stream(
        self, request: web.Request, upstream_url: str, mime: str, stream_id: str,
        session: StreamSession | None = None,
    ) -> web.StreamResponse:
        """Proxy an upstream HTTPS URL over HTTP with Content-Length."""
        is_radio = stream_id in self._radio_sessions
        timeout = aiohttp.ClientTimeout(total=None, sock_read=30) if is_radio else aiohttp.ClientTimeout(total=600)
        cs = self._get_http_session()
        async with cs.get(upstream_url, timeout=timeout) as upstream:
                headers = {
                    "Content-Type": upstream.headers.get("Content-Type", mime),
                    "Accept-Ranges": "bytes",
                    "transferMode.dlna.org": "Streaming",
                }
                cl = upstream.headers.get("Content-Length")
                if cl:
                    headers["Content-Length"] = cl
                if session:
                    headers.update(self._tune_timing_headers(session))

                response = web.StreamResponse(headers=headers)
                await response.prepare(request)
                if session:
                    session.register_response(response)

                logger.info("proxy_stream_started", stream_id=stream_id, content_length=cl)

                try:
                    async for chunk in upstream.content.iter_chunked(65536):
                        if session and not session.active:
                            break
                        await response.write(chunk)
                    if not session or session.active:
                        await response.write_eof()
                except Exception:
                    logger.debug("proxy_stream_disconnected", stream_id=stream_id)
                finally:
                    if session:
                        session.unregister_response(response)

                return response

    async def _cleanup_loop(self) -> None:
        """Remove stale sessions every 60s."""
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            timeout = settings.http_session_timeout
            stale = [
                sid for sid, s in self._sessions.items()
                if now - s.last_activity > timeout and not s.client_connected.is_set()
            ]
            for sid in stale:
                logger.info("stream_session_expired", stream_id=sid)
                self.remove_session(sid)

    async def stop(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                logger.debug("stream_cleanup_task_cancelled")
            self._cleanup_task = None

        for session in list(self._sessions.values()):
            session.close()
        self._sessions.clear()
        self._file_paths.clear()

        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("http_streamer_stopped")

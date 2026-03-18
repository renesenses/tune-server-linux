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


def _build_wav_header(stream_info: AudioStreamInfo) -> bytes:
    """Build a WAV header for streaming (unknown final size).

    Uses 0xFFFFFFFF as file/data sizes so DLNA renderers can parse the
    header and play the PCM data that follows.
    """
    channels = stream_info.channels or 2
    sample_rate = stream_info.sample_rate or 44100
    bit_depth = stream_info.bit_depth or 16
    byte_rate = sample_rate * channels * (bit_depth // 8)
    block_align = channels * (bit_depth // 8)
    data_size = 0x7FFFFFFF  # large placeholder
    file_size = data_size + 36  # 44 - 8

    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        file_size,
        b"WAVE",
        b"fmt ",
        16,            # fmt chunk size
        1,             # PCM format
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bit_depth,
        b"data",
        data_size,
    )


class StreamSession:
    """A single audio stream session."""

    def __init__(self, stream_id: str, stream_info: AudioStreamInfo) -> None:
        self.stream_id = stream_id
        self.stream_info = stream_info
        self._chunks: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=256)
        self.active = True
        self.client_connected = asyncio.Event()  # set when renderer makes first HTTP request
        self.created_at = time.monotonic()
        self.last_activity = time.monotonic()

    async def put(self, data: bytes) -> None:
        if self.active:
            self.last_activity = time.monotonic()
            await self._chunks.put(data)

    async def get(self) -> Optional[bytes]:
        return await self._chunks.get()

    def close(self) -> None:
        self.active = False
        try:
            self._chunks.put_nowait(None)
        except asyncio.QueueFull:
            pass


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
        self._sessions: dict[str, StreamSession] = {}
        self._file_paths: dict[str, str] = {}  # stream_id -> file_path for passthrough
        self._proxy_urls: dict[str, str] = {}  # stream_id -> upstream HTTPS URL for proxy
        self._cleanup_task: asyncio.Task | None = None

    @property
    def port(self) -> int:
        return self._port

    def create_session(self, stream_info: AudioStreamInfo, file_path: str | None = None) -> str:
        stream_id = str(uuid.uuid4())
        self._sessions[stream_id] = StreamSession(stream_id, stream_info)
        if file_path:
            self._file_paths[stream_id] = file_path
        logger.info("stream_session_created", stream_id=stream_id)
        return stream_id

    def get_session(self, stream_id: str) -> Optional[StreamSession]:
        return self._sessions.get(stream_id)

    def create_proxy_session(self, upstream_url: str, stream_info: AudioStreamInfo) -> str:
        """Create a session that proxies an upstream HTTPS URL over HTTP."""
        stream_id = str(uuid.uuid4())
        self._sessions[stream_id] = StreamSession(stream_id, stream_info)
        self._proxy_urls[stream_id] = upstream_url
        logger.info("proxy_session_created", stream_id=stream_id, url=upstream_url[:80])
        return stream_id

    def remove_session(self, stream_id: str) -> None:
        session = self._sessions.pop(stream_id, None)
        if session:
            session.close()
        self._file_paths.pop(stream_id, None)
        self._proxy_urls.pop(stream_id, None)

    def _resolve_mime(self, stream_id: str, session) -> str:
        """Return the correct MIME type, using file extension for DSD."""
        if session.stream_info.format == AudioFormat.DSD:
            file_path = self._file_paths.get(stream_id, "")
            return dsd_mime_from_extension(file_path)
        return mime_type_for_format(session.stream_info.format)

    def get_stream_url(self, stream_id: str, server_ip: str) -> str:
        session = self._sessions.get(stream_id)
        if not session:
            return ""
        ext = session.stream_info.format.value if hasattr(session.stream_info.format, 'value') else session.stream_info.format
        return f"http://{server_ip}:{self._port}/stream/{stream_id}.{ext}"

    async def start(self) -> None:
        self._app = web.Application()
        self._app.router.add_route("HEAD", "/stream/{stream_id}", self._handle_head)
        self._app.router.add_route("GET", "/stream/{stream_id}", self._handle_stream)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("http_streamer_started", host=self._host, port=self._port)

    async def _handle_head(self, request: web.Request) -> web.Response:
        stream_id = request.match_info["stream_id"].split(".")[0]
        session = self._sessions.get(stream_id)

        if not session:
            return web.Response(status=404)

        mime = self._resolve_mime(stream_id, session)
        headers = {
            "Content-Type": mime,
            "Accept-Ranges": "bytes",
            "transferMode.dlna.org": "Streaming",
            "contentFeatures.dlna.org": "",
        }

        # For proxy sessions, fetch Content-Length from upstream
        proxy_url = self._proxy_urls.get(stream_id)
        if proxy_url:
            try:
                async with aiohttp.ClientSession() as cs:
                    async with cs.head(proxy_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        cl = resp.headers.get("Content-Length")
                        if cl:
                            headers["Content-Length"] = cl
                        upstream_ct = resp.headers.get("Content-Type")
                        if upstream_ct:
                            headers["Content-Type"] = upstream_ct
            except Exception:
                pass
        elif session.stream_info.file_size:
            headers["Content-Length"] = str(session.stream_info.file_size)

        return web.Response(headers=headers)

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        stream_id = request.match_info["stream_id"].split(".")[0]
        session = self._sessions.get(stream_id)

        if not session:
            return web.Response(status=404)

        mime = self._resolve_mime(stream_id, session)

        # Signal that the renderer has connected
        session.client_connected.set()

        # Check for file-based passthrough with Range support
        file_path = self._file_paths.get(stream_id)
        if file_path and session.stream_info.file_size:
            try:
                return await self._serve_file(request, file_path, mime, session.stream_info.file_size)
            except Exception:
                logger.debug("stream_client_disconnected", stream_id=stream_id)
                return web.Response(status=499)  # client closed

        # Proxy mode: relay upstream HTTPS content over HTTP
        proxy_url = self._proxy_urls.get(stream_id)
        if proxy_url:
            try:
                return await self._proxy_stream(request, proxy_url, mime, stream_id)
            except Exception:
                logger.debug("proxy_client_disconnected", stream_id=stream_id)
                return web.Response(status=499)

        # Streaming mode
        response = web.StreamResponse(
            headers={
                "Content-Type": mime,
                "transferMode.dlna.org": "Streaming",
                "Cache-Control": "no-cache",
            }
        )
        await response.prepare(request)

        try:
            # Prepend WAV header when streaming decoded PCM as WAV
            if session.stream_info.format == AudioFormat.WAV:
                wav_header = _build_wav_header(session.stream_info)
                await response.write(wav_header)

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
            try:
                await response.write_eof()
            except Exception:
                pass

        return response

    async def _serve_file(
        self, request: web.Request, file_path: str, mime: str, file_size: int
    ) -> web.StreamResponse:
        """Serve a file with Range request support for DLNA."""
        range_header = request.headers.get("Range")

        if range_header:
            try:
                range_spec = range_header.replace("bytes=", "")
                start_str, end_str = range_spec.split("-")
                start = int(start_str) if start_str else 0
                end = int(end_str) if end_str else file_size - 1
                length = end - start + 1

                response = web.StreamResponse(
                    status=206,
                    headers={
                        "Content-Type": mime,
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Content-Length": str(length),
                        "Accept-Ranges": "bytes",
                        "transferMode.dlna.org": "Streaming",
                    },
                )
                await response.prepare(request)

                try:
                    with open(file_path, "rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk_size = min(65536, remaining)
                            chunk = f.read(chunk_size)
                            if not chunk:
                                break
                            await response.write(chunk)
                            remaining -= len(chunk)

                    await response.write_eof()
                except Exception:
                    logger.debug("serve_file_client_disconnected", stream_id=request.match_info.get("stream_id"))
                return response
            except (ValueError, OSError):
                pass

        # Full file response
        response = web.StreamResponse(
            headers={
                "Content-Type": mime,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "transferMode.dlna.org": "Streaming",
            },
        )
        await response.prepare(request)

        try:
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    await response.write(chunk)

            await response.write_eof()
        except Exception:
            logger.debug("serve_file_client_disconnected", stream_id=request.match_info.get("stream_id"))
        return response

    async def _proxy_stream(
        self, request: web.Request, upstream_url: str, mime: str, stream_id: str,
    ) -> web.StreamResponse:
        """Proxy an upstream HTTPS URL over HTTP with Content-Length."""
        async with aiohttp.ClientSession() as cs:
            async with cs.get(upstream_url, timeout=aiohttp.ClientTimeout(total=600)) as upstream:
                headers = {
                    "Content-Type": upstream.headers.get("Content-Type", mime),
                    "Accept-Ranges": "bytes",
                    "transferMode.dlna.org": "Streaming",
                }
                cl = upstream.headers.get("Content-Length")
                if cl:
                    headers["Content-Length"] = cl

                response = web.StreamResponse(headers=headers)
                await response.prepare(request)

                logger.info("proxy_stream_started", stream_id=stream_id, content_length=cl)

                try:
                    async for chunk in upstream.content.iter_chunked(65536):
                        await response.write(chunk)
                    await response.write_eof()
                except Exception:
                    logger.debug("proxy_stream_disconnected", stream_id=stream_id)

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
                pass
            self._cleanup_task = None

        for session in list(self._sessions.values()):
            session.close()
        self._sessions.clear()
        self._file_paths.clear()

        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("http_streamer_stopped")

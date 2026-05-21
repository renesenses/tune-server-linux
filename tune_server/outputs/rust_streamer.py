"""Rust sidecar streamer — manages a standalone Rust process for HTTP audio streaming.

Provides the same public API as HttpAudioStreamer so output classes (DLNA, Chromecast,
BluOS, etc.) can use it as a drop-in replacement.  The sidecar runs on the same port
(default 8080) and handles file serving + HTTPS-to-HTTP proxy with Range support.

UPnP routes and Deezer proxy remain in the Python aiohttp streamer for now.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import aiohttp
import structlog

from tune_server.audio.formats import dsd_mime_from_extension, mime_type_for_format
from tune_server.config import settings
from tune_server.models import AudioFormat, AudioStreamInfo
from tune_server.outputs.http_streamer import HttpAudioStreamer, StreamSession

logger = structlog.get_logger()


def _find_sidecar_binary() -> str | None:
    """Locate the tune-sidecar binary, checking common paths."""
    # 1. Explicit env override
    env_path = os.environ.get("TUNE_SIDECAR_BIN")
    if env_path and os.path.isfile(env_path):
        return env_path

    candidates: list[Path] = []

    # 2. Cargo release build in the project tree
    project_root = Path(__file__).resolve().parents[2]
    candidates.append(project_root / "target" / "release" / "tune-sidecar")

    # 3. Cargo debug build
    candidates.append(project_root / "target" / "debug" / "tune-sidecar")

    # 4. Next to the Python binary (PyInstaller bundle)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "tune-sidecar")

    for path in candidates:
        if path.is_file():
            return str(path)

    # 5. Anywhere on PATH
    on_path = shutil.which("tune-sidecar")
    if on_path:
        return on_path

    return None


class RustSidecarStreamer:
    """Manages a Rust sidecar process for HTTP audio streaming.

    Exposes the same API as HttpAudioStreamer so it can be used as a
    drop-in replacement by output classes.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        self._host = host
        self._port = port
        self._process: asyncio.subprocess.Process | None = None
        self._http: aiohttp.ClientSession | None = None
        self._base_url = f"http://127.0.0.1:{port}"
        self._route_callbacks: list = []
        self._healthy = False
        # Local tracking for get_session / set_session_metadata / get_stream_url
        self._sessions: dict[str, StreamSession] = {}

    @property
    def port(self) -> int:
        return self._port

    @property
    def app(self):
        """Not available — sidecar has no aiohttp app.  Returns None so
        callers that check ``if streamer.app`` skip gracefully."""
        return None

    def on_app_created(self, callback) -> None:
        """Store callbacks — they will be executed on the fallback Python
        streamer if the sidecar cannot be started."""
        self._route_callbacks.append(callback)

    # ─── Session management ──────────────────────────────────────

    def _resolve_mime(self, stream_info: AudioStreamInfo, file_path: str = "") -> str:
        if stream_info.format == AudioFormat.DSD:
            return dsd_mime_from_extension(file_path)
        return mime_type_for_format(stream_info.format)

    def _http_session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def _post(self, path: str, payload: dict) -> dict:
        cs = self._http_session()
        async with cs.post(f"{self._base_url}{path}", json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _delete(self, path: str) -> None:
        cs = self._http_session()
        async with cs.delete(f"{self._base_url}{path}", timeout=aiohttp.ClientTimeout(total=5)) as resp:
            pass  # 204 or 404 both fine

    def create_session(
        self,
        stream_info: AudioStreamInfo,
        file_path: str | None = None,
        bit_perfect: bool = False,
        max_chunks: int = 256,
    ) -> str:
        """Create a file-serving session.  Synchronous API (matches HttpAudioStreamer).

        If the sidecar is healthy AND a file_path is provided, the session is
        delegated to Rust.  Otherwise falls back to a local stub.
        """
        stream_id = str(uuid.uuid4())
        mime = self._resolve_mime(stream_info, file_path or "")

        if self._healthy and file_path and stream_info.file_size:
            # Fire-and-forget async call to the sidecar
            asyncio.get_event_loop().create_task(
                self._create_file_session_async(stream_id, stream_info, file_path, bit_perfect, mime)
            )
        # Keep a local stub so get_session / get_stream_url work immediately
        self._sessions[stream_id] = StreamSession(stream_id, stream_info, bit_perfect=bit_perfect, max_chunks=max_chunks)
        if file_path:
            self._sessions[stream_id].file_served = True
        return stream_id

    async def _create_file_session_async(
        self, stream_id: str, info: AudioStreamInfo, file_path: str,
        bit_perfect: bool, mime: str,
    ) -> None:
        try:
            ext = info.format.value if hasattr(info.format, "value") else info.format
            await self._post("/sessions/file", {
                "format": ext,
                "mime_type": mime,
                "sample_rate": info.sample_rate or 44100,
                "bit_depth": info.bit_depth or 16,
                "channels": info.channels or 2,
                "file_size": info.file_size,
                "file_path": file_path,
                "bit_perfect": bit_perfect,
            })
            logger.debug("rust_file_session_created", stream_id=stream_id)
        except Exception as e:
            logger.warning("rust_file_session_failed", stream_id=stream_id, error=str(e))

    def create_proxy_session(
        self,
        upstream_url: str,
        stream_info: AudioStreamInfo,
        bit_perfect: bool = False,
    ) -> str:
        stream_id = str(uuid.uuid4())
        mime = self._resolve_mime(stream_info)

        if self._healthy:
            asyncio.get_event_loop().create_task(
                self._create_proxy_session_async(stream_id, stream_info, upstream_url, mime, is_radio=False)
            )
        self._sessions[stream_id] = StreamSession(stream_id, stream_info, bit_perfect=bit_perfect)
        return stream_id

    def create_radio_proxy_session(
        self,
        upstream_url: str,
        stream_info: AudioStreamInfo,
    ) -> str:
        stream_id = str(uuid.uuid4())
        mime = self._resolve_mime(stream_info)

        if self._healthy:
            asyncio.get_event_loop().create_task(
                self._create_proxy_session_async(stream_id, stream_info, upstream_url, mime, is_radio=True)
            )
        self._sessions[stream_id] = StreamSession(stream_id, stream_info)
        return stream_id

    async def _create_proxy_session_async(
        self, stream_id: str, info: AudioStreamInfo, upstream_url: str,
        mime: str, is_radio: bool,
    ) -> None:
        try:
            ext = info.format.value if hasattr(info.format, "value") else info.format
            await self._post("/sessions/proxy", {
                "format": ext,
                "mime_type": mime,
                "sample_rate": info.sample_rate or 44100,
                "bit_depth": info.bit_depth or 16,
                "channels": info.channels or 2,
                "file_size": info.file_size,
                "upstream_url": upstream_url,
                "is_radio": is_radio,
            })
            logger.debug("rust_proxy_session_created", stream_id=stream_id)
        except Exception as e:
            logger.warning("rust_proxy_session_failed", stream_id=stream_id, error=str(e))

    def get_session(self, stream_id: str) -> StreamSession | None:
        return self._sessions.get(stream_id)

    def set_session_metadata(
        self,
        stream_id: str,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        cover_url: str | None = None,
    ) -> None:
        session = self._sessions.get(stream_id)
        if session:
            session.track_title = title
            session.track_artist = artist
            session.track_album = album
            session.track_cover_url = cover_url

    def get_stream_url(self, stream_id: str, server_ip: str) -> str:
        session = self._sessions.get(stream_id)
        if not session:
            return ""
        ext = session.stream_info.format.value if hasattr(session.stream_info.format, "value") else session.stream_info.format
        return f"http://{server_ip}:{self._port}/stream/{stream_id}.{ext}"

    def remove_session(self, stream_id: str) -> None:
        session = self._sessions.pop(stream_id, None)
        if session:
            session.close()
        if self._healthy:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._delete(f"/sessions/{stream_id}"))
            except RuntimeError:
                pass

    # ─── Lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        """Start the Rust sidecar process.  If it fails, the caller should
        fall back to HttpAudioStreamer."""
        binary = _find_sidecar_binary()
        if not binary:
            raise FileNotFoundError("tune-sidecar binary not found")

        env = {
            **os.environ,
            "TUNE_STREAM_PORT": str(self._port),
            "TUNE_SIDECAR_LOG": os.environ.get("TUNE_SIDECAR_LOG", "info"),
        }

        self._process = await asyncio.create_subprocess_exec(
            binary,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("rust_sidecar_spawned", pid=self._process.pid, port=self._port, binary=binary)

        # Wait for the sidecar to be healthy (up to 5s)
        if await self._wait_healthy(timeout=5.0):
            self._healthy = True
            logger.info("rust_sidecar_ready", port=self._port)
        else:
            await self._kill()
            raise RuntimeError("Rust sidecar failed health check")

        # Start log relay in background
        asyncio.create_task(self._relay_stderr())

    async def _wait_healthy(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        cs = self._http_session()
        while time.monotonic() < deadline:
            try:
                async with cs.get(f"{self._base_url}/health", timeout=aiohttp.ClientTimeout(total=1)) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.15)
        return False

    async def _relay_stderr(self) -> None:
        """Read the sidecar's stderr and log it through structlog."""
        if not self._process or not self._process.stderr:
            return
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.debug("rust_sidecar", output=text)

    async def _kill(self) -> None:
        if self._process and self._process.returncode is None:
            try:
                self._process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self._process.kill()
            except ProcessLookupError:
                pass
        self._process = None
        self._healthy = False

    async def stop(self) -> None:
        for session in list(self._sessions.values()):
            session.close()
        self._sessions.clear()

        await self._kill()

        if self._http and not self._http.closed:
            await self._http.close()
            self._http = None

        logger.info("rust_sidecar_stopped")


def create_streamer(host: str = "0.0.0.0", port: int = 8080) -> RustSidecarStreamer | HttpAudioStreamer:
    """Factory: return a RustSidecarStreamer if available, HttpAudioStreamer otherwise.

    Caller must still call await streamer.start() and handle the fallback.
    This function only instantiates — it does NOT start the sidecar.
    """
    engine = os.environ.get("TUNE_STREAMER_ENGINE", settings.streamer_engine).lower()
    if engine == "python":
        logger.info("streamer_engine_forced_python")
        return HttpAudioStreamer(host=host, port=port)

    if engine == "rust" or engine == "auto":
        binary = _find_sidecar_binary()
        if binary:
            logger.info("streamer_engine_rust", binary=binary)
            return RustSidecarStreamer(host=host, port=port)
        if engine == "rust":
            logger.error("streamer_engine_rust_requested_but_binary_not_found")
        else:
            logger.info("streamer_engine_fallback_python", reason="binary_not_found")

    return HttpAudioStreamer(host=host, port=port)

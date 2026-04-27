from __future__ import annotations

import asyncio
import shutil
import struct
from pathlib import Path
from typing import Optional

import structlog
from aiohttp import web

from tune_server.spotify_connect.daemon import (
    LibrespotDaemon,
    PCM_BITS_PER_SAMPLE,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
)

logger = structlog.get_logger()


def _wav_header(byte_rate_size: int = 0xFFFFFFFF) -> bytes:
    """Streaming WAV header: declares unknown size (0xFFFFFFFF) so renderers
    parse the header and read the PCM that follows without expecting EOF."""
    channels = PCM_CHANNELS
    sample_rate = PCM_SAMPLE_RATE
    bit_depth = PCM_BITS_PER_SAMPLE
    byte_rate = sample_rate * channels * (bit_depth // 8)
    block_align = channels * (bit_depth // 8)
    return (
        b"RIFF" + struct.pack("<I", byte_rate_size)
        + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH",
                                16,                  # subchunk1 size
                                1,                   # PCM format
                                channels,
                                sample_rate,
                                byte_rate,
                                block_align,
                                bit_depth)
        + b"data" + struct.pack("<I", byte_rate_size)
    )


class SpotifyConnectRelay:
    """HTTP relay that exposes librespot's PCM output as a WAV stream.

    Pipeline:
        librespot --backend pipe (stdout: raw S16LE 44.1kHz stereo)
            -> SpotifyConnectRelay (multiplexes to N HTTP clients)
            -> GET http://<host>:<port>/spotify-connect/stream.wav
    """

    def __init__(self, daemon: LibrespotDaemon, host: str = "0.0.0.0", port: int = 8082) -> None:
        self._daemon = daemon
        self._host = host
        self._port = port
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._subscribers: set[asyncio.Queue[bytes]] = set()
        self._fanout_task: asyncio.Task | None = None

    @property
    def stream_url_path(self) -> str:
        return "/spotify-connect/stream.wav"

    def url_for(self, server_ip: str) -> str:
        return f"http://{server_ip}:{self._port}{self.stream_url_path}"

    async def start(self) -> None:
        if self._app is not None:
            return
        self._app = web.Application()
        # aiohttp auto-registers HEAD for every GET route — adding it
        # explicitly would clash. We only register GET; the auto-HEAD reuses
        # the same handler but discards the response body.
        self._app.router.add_get(self.stream_url_path, self._handle_stream)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port, reuse_address=True)
        await site.start()
        self._fanout_task = asyncio.create_task(self._fanout_loop())
        logger.info("spotify_connect_relay_started", host=self._host, port=self._port)

    async def stop(self) -> None:
        if self._fanout_task:
            self._fanout_task.cancel()
            self._fanout_task = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        for q in list(self._subscribers):
            await q.put(b"")  # signal EOF to active responses
        self._subscribers.clear()
        logger.info("spotify_connect_relay_stopped")

    async def _fanout_loop(self) -> None:
        """Read PCM chunks from librespot and broadcast to all subscribers."""
        while True:
            chunk = await self._daemon.read_pcm_chunk(8192)
            if not chunk:
                # daemon stopped — wait briefly before retrying
                await asyncio.sleep(0.5)
                if not self._daemon.is_running:
                    break
                continue
            for q in list(self._subscribers):
                if q.full():
                    # subscriber is too slow — drop the chunk for them only
                    continue
                q.put_nowait(chunk)

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/wav",
                "transferMode.dlna.org": "Streaming",
                "Cache-Control": "no-cache",
            },
        )
        await response.prepare(request)
        await response.write(_wav_header())

        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        self._subscribers.add(q)
        try:
            while True:
                chunk = await q.get()
                if not chunk:
                    break
                await response.write(chunk)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self._subscribers.discard(q)
        return response

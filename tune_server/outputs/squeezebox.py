"""Squeezebox (Lyrion/Logitech Media Server) audio output.

Audio is streamed via HTTP URL injection into the LMS CLI (same pull model
as DLNA/Chromecast/BluOS).  Control commands (play, pause, stop, volume,
next, prev, seek) are sent over the LMS CLI telnet-style protocol on TCP
port 9090.

Reference: https://lyrion.org/reference/cli/
"""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import Optional

import structlog

from tune_server.audio.formats import AudioCapabilities, AudioFormat, SQUEEZEBOX_CAPABILITIES
from tune_server.models import AudioStreamInfo, Track
from tune_server.outputs.base import OutputTarget

logger = structlog.get_logger()

_SQUEEZEBOX_DIRECT_FORMATS = {AudioFormat.FLAC, AudioFormat.MP3, AudioFormat.AAC, AudioFormat.OGG, AudioFormat.WAV}


class _LmsCli:
    """Minimal async LMS CLI client (telnet protocol on port 9090).

    Each command is a single line terminated by ``\\n``.  The server echoes
    the command back as the response line.  Player-specific commands are
    prefixed with the player MAC address (URL-encoded).
    """

    def __init__(self, host: str, port: int = 9090) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._writer and not self._writer.is_closing():
            return
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            timeout=5,
        )

    async def close(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def command(self, cmd: str) -> str:
        """Send a raw CLI command and return the response line."""
        async with self._lock:
            await self.connect()
            assert self._writer is not None and self._reader is not None
            self._writer.write((cmd + "\n").encode("utf-8"))
            await self._writer.drain()
            line = await asyncio.wait_for(self._reader.readline(), timeout=5)
            return urllib.parse.unquote(line.decode("utf-8").strip())

    async def player_command(self, player_id: str, cmd: str) -> str:
        """Send a player-scoped CLI command."""
        encoded_id = urllib.parse.quote(player_id, safe="")
        return await self.command(f"{encoded_id} {cmd}")


class SqueezeboxOutput(OutputTarget):
    """Stream audio to a Squeezebox player via Lyrion Media Server CLI."""

    def __init__(
        self,
        lms_host: str,
        player_id: str,
        streamer,
        server_ip: str,
        lms_port: int = 9090,
        device_name: str = "Squeezebox",
    ) -> None:
        self._lms_host = lms_host
        self._lms_port = lms_port
        self._player_id = player_id
        self._streamer = streamer
        self._server_ip = server_ip
        self._name = device_name
        self._available = True
        self._stream_id: str | None = None
        self._direct_url: bool = False
        self._cli = _LmsCli(lms_host, lms_port)

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> AudioCapabilities:
        return SQUEEZEBOX_CAPABILITIES

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def is_direct_url(self) -> bool:
        return self._direct_url

    def supports_direct_url(self, track: Track) -> bool:
        if not track or not track.file_path:
            return False
        if not track.file_path.startswith(("http://", "https://")):
            return False
        fmt = AudioFormat(track.format) if track.format else None
        return fmt in _SQUEEZEBOX_DIRECT_FORMATS

    # ------------------------------------------------------------------
    # OutputTarget interface
    # ------------------------------------------------------------------

    async def start(self, stream_info: AudioStreamInfo, track: Optional[Track] = None) -> None:
        if self._stream_id:
            self._streamer.remove_session(self._stream_id)
            self._stream_id = None
        self._direct_url = False

        # Determine the URL to inject into LMS
        if track and self.supports_direct_url(track):
            url = track.file_path
            self._direct_url = True
        elif track and track.file_path and track.file_path.startswith(("http://", "https://")):
            url = track.file_path
            self._direct_url = True
        else:
            file_path = track.file_path if track else None
            self._stream_id = self._streamer.create_session(stream_info, file_path)
            url = self._streamer.get_stream_url(self._stream_id, self._server_ip)

        title = track.title if track else "Audio"

        try:
            encoded_url = urllib.parse.quote(url, safe="")
            await self._cli.player_command(self._player_id, f"playlist play {encoded_url}")
            logger.info("squeezebox_playing", device=self._name, title=title, url=url[:80])
        except Exception:
            logger.exception("squeezebox_start_error", device=self._name)
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
            await self._cli.player_command(self._player_id, "pause 1")
        except Exception:
            logger.debug("squeezebox_pause_error", device=self._name)

    async def resume(self) -> None:
        try:
            await self._cli.player_command(self._player_id, "pause 0")
        except Exception:
            logger.debug("squeezebox_resume_error", device=self._name)

    async def stop(self) -> None:
        try:
            await self._cli.player_command(self._player_id, "stop")
        except Exception:
            pass
        if self._stream_id:
            self._streamer.remove_session(self._stream_id)
            self._stream_id = None
        self._direct_url = False

    async def set_volume(self, volume: float) -> None:
        try:
            level = int(max(0, min(100, volume * 100)))
            await self._cli.player_command(self._player_id, f"mixer volume {level}")
        except Exception:
            logger.debug("squeezebox_volume_error", device=self._name)

    async def seek(self, position_ms: int) -> bool:
        try:
            seconds = position_ms / 1000.0
            await self._cli.player_command(self._player_id, f"time {seconds:.1f}")
            logger.info("squeezebox_seek", device=self._name, position_ms=position_ms)
            return True
        except Exception:
            logger.debug("squeezebox_seek_error", device=self._name)
            return False

    async def get_position_ms(self) -> int:
        try:
            resp = await self._cli.player_command(self._player_id, "time ?")
            # Response: "<player_id> time <seconds>"
            parts = resp.rsplit(" ", 1)
            if len(parts) >= 2:
                secs = float(parts[-1])
                return int(secs * 1000)
        except Exception:
            pass
        return -1

    async def set_next_track(self, stream_info: AudioStreamInfo, track: Track) -> bool:
        """Enqueue the next track for gapless playback."""
        try:
            if track and self.supports_direct_url(track):
                url = track.file_path
            elif track and track.file_path and track.file_path.startswith(("http://", "https://")):
                url = track.file_path
            else:
                file_path = track.file_path if track else None
                sid = self._streamer.create_session(stream_info, file_path)
                url = self._streamer.get_stream_url(sid, self._server_ip)

            encoded_url = urllib.parse.quote(url, safe="")
            await self._cli.player_command(self._player_id, f"playlist add {encoded_url}")
            logger.info("squeezebox_next_enqueued", device=self._name, title=track.title if track else "?")
            return True
        except Exception:
            logger.debug("squeezebox_set_next_error", device=self._name, exc_info=True)
            return False

    async def close(self) -> None:
        await self.stop()
        await self._cli.close()

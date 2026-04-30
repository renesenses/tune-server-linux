from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

# librespot --backend pipe writes raw signed 16-bit little-endian PCM at 44.1kHz stereo.
PCM_SAMPLE_RATE = 44100
PCM_CHANNELS = 2
PCM_BITS_PER_SAMPLE = 16

# librespot logs events to stderr in a recognizable format. We parse the most
# useful ones; the full event protocol is documented at
# https://github.com/librespot-org/librespot/wiki/Player-Events.
_EVENT_RE = re.compile(
    r"player_event[:\s]+(?P<event>\w+)"
    r"(?:.*?track_id[:\s=]+(?P<track_id>[A-Za-z0-9]+))?",
    re.IGNORECASE,
)


class LibrespotDaemon:
    """Wraps a librespot subprocess in zeroconf (Spotify Connect) mode."""

    def __init__(
        self,
        device_name: str,
        binary_path: Path | str = "librespot",
        bitrate: int = 320,
        on_event: Optional[callable] = None,
    ) -> None:
        self._device_name = device_name
        self._binary_path = str(binary_path)
        self._bitrate = bitrate
        self._on_event = on_event
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        if self.is_running:
            return
        if not shutil.which(self._binary_path) and not Path(self._binary_path).exists():
            raise FileNotFoundError(f"librespot binary not found: {self._binary_path}")

        args = [
            self._binary_path,
            "--name", self._device_name,
            "--bitrate", str(self._bitrate),
            "--backend", "pipe",
            "--device-type", "speaker",
            "--disable-audio-cache",
        ]
        logger.info("librespot_starting", device_name=self._device_name, bitrate=self._bitrate)
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._read_stderr())
        logger.info("librespot_started", pid=self._process.pid)

    async def stop(self) -> None:
        if not self.is_running:
            return
        logger.info("librespot_stopping", pid=self._process.pid)
        self._process.terminate()
        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            self._process.kill()
            await self._process.wait()
        if self._stderr_task:
            self._stderr_task.cancel()
        logger.info("librespot_stopped")

    async def read_pcm_chunk(self, n_bytes: int = 4096) -> bytes:
        if not self.is_running:
            return b""
        return await self._process.stdout.read(n_bytes)

    async def _read_stderr(self) -> None:
        assert self._process and self._process.stderr
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            match = _EVENT_RE.search(text)
            if match and self._on_event:
                evt = match.group("event")
                track_id = match.group("track_id")
                try:
                    await self._on_event(event=evt, track_id=track_id, raw=text)
                except Exception as exc:
                    logger.warning("librespot_event_handler_failed", error=str(exc))
            else:
                logger.debug("librespot_stderr", line=text)

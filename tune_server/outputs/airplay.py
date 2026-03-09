from __future__ import annotations

import asyncio

import structlog

from tune_server.audio.formats import AIRPLAY_CAPABILITIES, AudioCapabilities
from tune_server.models import AudioStreamInfo, Track
from tune_server.outputs.base import OutputTarget

logger = structlog.get_logger()


class AirPlayOutput(OutputTarget):
    """AirPlay output using pyatv for RAOP streaming.

    pyatv's stream_file() handles all encoding/streaming internally,
    so we pass it the file path directly and let it do the work.
    """

    def __init__(self, atv_device: object, device_name: str = "AirPlay") -> None:
        self._atv = atv_device  # pyatv.interface.AppleTV
        self._device_name = device_name
        self._available = True
        self._volume: float = 0.5
        self._stream_task: asyncio.Task | None = None

    @property
    def name(self) -> str:
        return self._device_name

    @property
    def capabilities(self) -> AudioCapabilities:
        return AIRPLAY_CAPABILITIES

    @property
    def is_available(self) -> bool:
        return self._available

    def supports_direct_url(self, track: Track) -> bool:
        """AirPlay streams files natively via pyatv — always bypass the pipeline."""
        return True

    async def start(self, stream_info: AudioStreamInfo, track: Track | None = None) -> None:
        if not track or not track.file_path:
            logger.error("airplay_no_file", device=self._device_name)
            return

        try:
            stream = self._atv.stream

            # Build metadata if available
            metadata = None
            try:
                from pyatv.interface import MediaMetadata
                metadata = MediaMetadata(
                    title=track.title or None,
                    artist=track.artist_name or None,
                    album=track.album_title or None,
                    duration=track.duration_ms / 1000.0 if track.duration_ms else None,
                )
            except Exception:
                pass

            # stream_file handles encoding + RAOP streaming internally
            logger.info("airplay_streaming", device=self._device_name, track=track.title)
            self._stream_task = asyncio.create_task(
                stream.stream_file(track.file_path, metadata=metadata)
            )

            # Wait briefly to catch immediate failures (auth errors, connection refused).
            # If the task is still running after the timeout, streaming is in progress.
            done, _ = await asyncio.wait({self._stream_task}, timeout=5.0)
            if done:
                task = done.pop()
                exc = task.exception()
                if exc:
                    self._stream_task = None
                    self._available = False
                    raise RuntimeError(
                        f"AirPlay streaming failed on '{self._device_name}': {exc}"
                    ) from exc

            self._available = True

        except RuntimeError:
            raise
        except Exception:
            logger.exception("airplay_start_error", device=self._device_name)
            self._available = False
            raise RuntimeError(
                f"AirPlay output unavailable: {self._device_name}"
            )

    async def write(self, data: bytes) -> None:
        # Not used — pyatv streams the file itself
        pass

    async def flush(self) -> None:
        pass

    async def _remote_call(self, label: str, coro) -> bool:
        """Call a remote/audio coroutine with timeout."""
        try:
            await asyncio.wait_for(coro, timeout=10)
            self._available = True
            return True
        except asyncio.TimeoutError:
            logger.warning("airplay_timeout", action=label, device=self._device_name)
            return False
        except Exception:
            logger.debug("airplay_call_error", action=label, device=self._device_name)
            return False

    async def pause(self) -> None:
        rc = self._atv.remote_control
        await self._remote_call("pause", rc.pause())

    async def resume(self) -> None:
        rc = self._atv.remote_control
        await self._remote_call("play", rc.play())

    async def stop(self) -> None:
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stream_task = None

        rc = self._atv.remote_control
        await self._remote_call("stop", rc.stop())

    async def set_volume(self, volume: float) -> None:
        self._volume = volume
        audio = self._atv.audio
        await self._remote_call("set_volume", audio.set_volume(volume * 100))

    async def get_position_ms(self) -> int:
        """Query the AirPlay device's current playback position."""
        try:
            playing = await asyncio.wait_for(self._atv.metadata.playing(), timeout=5)
            if playing and playing.position is not None:
                return int(playing.position * 1000)
        except asyncio.TimeoutError:
            logger.debug("airplay_position_timeout", device=self._device_name)
        except Exception:
            logger.debug("airplay_position_error", device=self._device_name)
        return -1

    async def close(self) -> None:
        await self.stop()
        try:
            self._atv.close()
        except Exception:
            pass

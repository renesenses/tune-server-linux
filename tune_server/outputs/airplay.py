from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import structlog

from tune_server.audio.formats import AIRPLAY_CAPABILITIES, AudioCapabilities
from tune_server.models import AudioStreamInfo, Track
from tune_server.outputs.base import OutputTarget

logger = structlog.get_logger()

# Exponential backoff schedule for connection retries (seconds)
_RETRY_DELAYS = (2, 5, 10, 30)


class AirPlayOutput(OutputTarget):
    """AirPlay output using pyatv for RAOP streaming.

    pyatv's stream_file() handles all encoding/streaming internally,
    so we pass it the file path directly and let it do the work.
    For HTTP streams (radios), we use the HTTP streamer as intermediary.
    """

    def __init__(self, atv_device: object, device_name: str = "AirPlay",
                 streamer=None, server_ip: str = "") -> None:
        self._atv = atv_device  # pyatv.interface.AppleTV
        self._device_name = device_name
        self._available = True
        self._volume: float = 0.5
        self._stream_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._temp_wav: Path | None = None
        self._streamer = streamer
        self._server_ip = server_ip
        self._stream_id: str | None = None

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
        """AirPlay streams local files natively via pyatv — not streaming URLs."""
        if not track.file_path:
            return False
        # Streaming URLs need the pipeline to decode
        if track.file_path.startswith("http://") or track.file_path.startswith("https://"):
            return False
        return True

    async def _transcode_to_wav(self, file_path: str) -> Path:
        """Transcode a file to WAV via FFmpeg for pyatv compatibility."""
        from tune_server.config import settings
        from tune_server.utils.network import subprocess_hide_window
        self._cleanup_temp_wav()
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        import os
        os.close(fd)
        tmp_path = Path(tmp)
        cmd = [
            settings.ffmpeg_path, "-hide_banner", "-loglevel", "error",
            "-i", file_path,
            "-f", "wav", "-acodec", "pcm_s16le",
            "-ar", "44100", "-ac", "2",
            "-y", str(tmp_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            **subprocess_hide_window(),
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"FFmpeg transcode failed: {stderr.decode()[:200]}")
        self._temp_wav = tmp_path
        return tmp_path

    def _cleanup_temp_wav(self) -> None:
        if self._temp_wav and self._temp_wav.exists():
            self._temp_wav.unlink(missing_ok=True)
            self._temp_wav = None

    async def start(self, stream_info: AudioStreamInfo, track: Track | None = None) -> None:
        if not track or not track.file_path:
            logger.error("airplay_no_file", device=self._device_name)
            return

        await self.stop()

        try:
            stream = self._atv.stream

            metadata = None
            try:
                from pyatv.interface import MediaMetadata
                metadata = MediaMetadata(
                    title=track.title or None,
                    artist=track.artist_name or None,
                    album=track.album_title or None,
                    duration=track.duration_ms / 1000.0 if track.duration_ms else None,
                )
            except Exception as e:
                logger.debug("airplay_metadata_build_failed", error=str(e))

            logger.info("airplay_streaming", device=self._device_name, track=track.title,
                        file=track.file_path[:80])

            is_http = track.file_path.startswith("http://") or track.file_path.startswith("https://")

            if is_http and self._streamer:
                # HTTP streams (radios) can't be passed to pyatv directly.
                # Use the HTTP streamer: pipeline decodes → streamer serves WAV → pyatv plays the local URL.
                self._stream_id = self._streamer.create_session(stream_info, track.file_path)
                local_url = self._streamer.get_stream_url(self._stream_id, self._server_ip)
                logger.info("airplay_via_streamer", device=self._device_name, url=local_url[:80])
                self._stream_task = asyncio.create_task(
                    stream.stream_file(local_url, metadata=metadata)
                )
                done, _ = await asyncio.wait({self._stream_task}, timeout=5.0)
                if done:
                    task = done.pop()
                    exc = task.exception()
                    if exc:
                        self._stream_task = None
                        raise RuntimeError(
                            f"AirPlay streamer failed on '{self._device_name}': {exc}"
                        ) from exc
            else:
                # Local files: try direct streaming, fall back to WAV transcode
                try:
                    self._stream_task = asyncio.create_task(
                        stream.stream_file(track.file_path, metadata=metadata)
                    )
                    done, _ = await asyncio.wait({self._stream_task}, timeout=5.0)
                    if done:
                        task = done.pop()
                        exc = task.exception()
                        if exc:
                            self._stream_task = None
                            raise exc
                except Exception as first_err:
                    if self._stream_task:
                        self._stream_task.cancel()
                        self._stream_task = None
                    logger.info("airplay_transcode_fallback", device=self._device_name,
                                reason=str(first_err)[:120])
                    wav_path = await self._transcode_to_wav(track.file_path)
                    self._stream_task = asyncio.create_task(
                        stream.stream_file(str(wav_path), metadata=metadata)
                    )
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

            try:
                audio = self._atv.audio
                await asyncio.wait_for(
                    audio.set_volume(self._volume * 100), timeout=5
                )
            except Exception:
                logger.debug("airplay_post_start_volume_failed", device=self._device_name)

        except RuntimeError:
            raise
        except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
            logger.warning(
                "airplay_connection_refused",
                device=self._device_name,
                error=str(exc),
            )
            self._available = False
            raise RuntimeError(
                f"AirPlay device refused connection: {self._device_name}"
            ) from exc
        except Exception:
            logger.exception("airplay_start_error", device=self._device_name)
            self._available = False
            raise RuntimeError(
                f"AirPlay output unavailable: {self._device_name}"
            )

    async def write(self, data: bytes) -> None:
        if self._stream_id and self._streamer:
            session = self._streamer.get_session(self._stream_id)
            if session:
                await session.put(data)

    async def flush(self) -> None:
        pass

    async def _remote_call(self, label: str, coro_fn, *, retries: bool = True) -> bool:
        """Call a remote/audio coroutine factory with timeout and exponential backoff.

        ``coro_fn`` must be a zero-argument callable that returns a new coroutine
        each time (e.g. ``lambda: rc.pause()``).  On transient failures (timeout,
        connection errors) the call is retried with exponential backoff (2s, 5s,
        10s, 30s) before giving up.
        """
        delays = _RETRY_DELAYS if retries else ()
        max_attempts = len(delays) + 1

        for attempt in range(1, max_attempts + 1):
            try:
                await asyncio.wait_for(coro_fn(), timeout=10)
                if not self._available:
                    logger.info(
                        "airplay_reconnected",
                        action=label,
                        device=self._device_name,
                        attempt=attempt,
                    )
                self._available = True
                return True
            except asyncio.TimeoutError:
                logger.warning(
                    "airplay_timeout",
                    action=label,
                    device=self._device_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
            except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
                logger.warning(
                    "airplay_connection_error",
                    action=label,
                    device=self._device_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=str(exc),
                )
            except Exception:
                logger.debug(
                    "airplay_call_error",
                    action=label,
                    device=self._device_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                # Unknown errors are not retried — bail immediately
                self._available = False
                return False

            # Schedule retry with backoff if attempts remain
            if attempt < max_attempts:
                delay = delays[attempt - 1]
                logger.info(
                    "airplay_retry_scheduled",
                    action=label,
                    device=self._device_name,
                    attempt=attempt,
                    next_retry_in=delay,
                )
                await asyncio.sleep(delay)

        self._available = False
        logger.warning(
            "airplay_retry_exhausted",
            action=label,
            device=self._device_name,
            attempts=max_attempts,
        )
        return False

    async def pause(self) -> None:
        rc = self._atv.remote_control
        await self._remote_call("pause", lambda: rc.pause())

    async def resume(self) -> None:
        rc = self._atv.remote_control
        await self._remote_call("play", lambda: rc.play())

    async def stop(self) -> None:
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception) as e:
                logger.debug("airplay_stream_task_cancel", error=str(e))
            self._stream_task = None

        self._cleanup_temp_wav()
        if self._stream_id and self._streamer:
            self._streamer.remove_session(self._stream_id)
            self._stream_id = None

        rc = self._atv.remote_control
        await self._remote_call("stop", lambda: rc.stop())

    async def set_volume(self, volume: float) -> None:
        self._volume = volume
        audio = self._atv.audio
        await self._remote_call("set_volume", lambda: audio.set_volume(volume * 100))

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
        self._cleanup_temp_wav()
        try:
            self._atv.close()
        except Exception as e:
            logger.debug("airplay_close_error", error=str(e))

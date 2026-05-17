"""Chromecast (Google Cast) audio output.

Requires: pychromecast (pip install pychromecast)
Audio is cast via HTTP URL (same pull model as DLNA).
"""

from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from tune_server.audio.formats import AudioCapabilities, AudioFormat, CHROMECAST_CAPABILITIES
from tune_server.models import AudioStreamInfo, Track
from tune_server.outputs.base import OutputTarget

logger = structlog.get_logger()

_CAST_DIRECT_FORMATS = {AudioFormat.FLAC, AudioFormat.MP3, AudioFormat.AAC, AudioFormat.OGG, AudioFormat.WAV}

_MIME_MAP = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "aac": "audio/mp4",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
}


class ChromecastOutput(OutputTarget):
    """Cast audio to a Google Cast device via HTTP URL."""

    def __init__(
        self,
        cast_device,
        streamer,
        server_ip: str,
        device_name: str = "Chromecast",
    ) -> None:
        self._cast = cast_device
        self._streamer = streamer
        self._server_ip = server_ip
        self._name = device_name
        self._available = True
        self._stream_id: str | None = None
        self._direct_url: bool = False
        self._last_content_id: str | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> AudioCapabilities:
        return CHROMECAST_CAPABILITIES

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
        return fmt in _CAST_DIRECT_FORMATS

    async def _cast_call(self, fn, *args, timeout: float = 10, **kwargs):
        return await asyncio.wait_for(
            asyncio.to_thread(fn, *args, **kwargs),
            timeout=timeout,
        )

    async def start(self, stream_info: AudioStreamInfo, track: Optional[Track] = None) -> None:
        if self._stream_id:
            self._streamer.remove_session(self._stream_id)
            self._stream_id = None
        self._direct_url = False

        mc = self._cast.media_controller

        # Determine URL and content type
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

        content_type = "audio/flac"
        if track and track.format:
            fmt = track.format.value if hasattr(track.format, "value") else str(track.format)
            content_type = _MIME_MAP.get(fmt, "audio/flac")

        title = track.title if track else "Audio"
        artist = track.artist_name if track else None
        album = track.album_title if track else None

        thumb = None
        if track and track.cover_path:
            if track.cover_path.startswith("http"):
                thumb = f"http://{self._server_ip}:8888/api/v1/library/artwork/proxy?url={track.cover_path}"
            else:
                filename = track.cover_path.split("/")[-1]
                thumb = f"http://{self._server_ip}:8888/api/v1/library/artwork/{filename}"

        metadata = {"metadataType": 3, "title": title}
        if artist:
            metadata["artist"] = artist
        if album:
            metadata["albumName"] = album

        try:
            await self._cast_call(
                mc.play_media, url, content_type,
                title=title, thumb=thumb, metadata=metadata,
            )
            await self._cast_call(mc.block_until_active, timeout=15)
            self._last_content_id = url
            logger.info("chromecast_playing", device=self._name, title=title, url=url[:80])
        except Exception:
            logger.exception("chromecast_start_error", device=self._name)
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
            await self._cast_call(self._cast.media_controller.pause)
        except Exception:
            logger.debug("chromecast_pause_error", device=self._name)

    async def resume(self) -> None:
        try:
            await self._cast_call(self._cast.media_controller.play)
        except Exception:
            logger.debug("chromecast_resume_error", device=self._name)

    async def stop(self) -> None:
        try:
            await self._cast_call(self._cast.media_controller.stop)
        except Exception:
            pass
        if self._stream_id:
            self._streamer.remove_session(self._stream_id)
            self._stream_id = None
        self._direct_url = False

    async def set_volume(self, volume: float) -> None:
        try:
            await self._cast_call(self._cast.set_volume, max(0.0, min(1.0, volume)))
        except Exception:
            logger.debug("chromecast_volume_error", device=self._name)

    async def seek(self, position_ms: int) -> bool:
        try:
            await self._cast_call(self._cast.media_controller.seek, position_ms / 1000.0)
            logger.info("chromecast_seek", device=self._name, position_ms=position_ms)
            return True
        except Exception:
            logger.debug("chromecast_seek_error", device=self._name)
            return False

    async def get_position_ms(self) -> int:
        try:
            status = self._cast.media_controller.status
            if status:
                # IDLE with idle_reason FINISHED = track ended
                if status.player_is_idle:
                    return -2
                if status.current_time is not None:
                    return int(status.current_time * 1000)
        except Exception:
            pass
        return -1

    async def set_next_track(self, stream_info: AudioStreamInfo, track: Track) -> bool:
        # Chromecast enqueue is unreliable (can replace current media on some
        # devices/firmware). Rely on auto-advance via _direct_url_monitor instead.
        return False

    def has_uri_changed(self) -> bool:
        try:
            status = self._cast.media_controller.status
            if status and status.content_id and self._last_content_id:
                return status.content_id != self._last_content_id
        except Exception:
            pass
        return False

    def sync_last_uri(self) -> None:
        try:
            status = self._cast.media_controller.status
            if status and status.content_id:
                self._last_content_id = status.content_id
        except Exception:
            pass

    async def close(self) -> None:
        await self.stop()
        try:
            if hasattr(self._cast, 'socket_client') and self._cast.socket_client:
                await self._cast_call(self._cast.socket_client.disconnect)
            await self._cast_call(self._cast.disconnect)
        except Exception:
            pass

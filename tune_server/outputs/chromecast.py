"""Chromecast (Google Cast) audio output.

Requires: pychromecast (pip install pychromecast)
Discovery is handled via zeroconf (already a dependency).
Audio is cast via HTTP URL (same mechanism as DLNA).
"""

from __future__ import annotations

import asyncio
from typing import Optional

import structlog

logger = structlog.get_logger()


class ChromecastOutput:
    """Cast audio to a Chromecast device via HTTP streaming URL."""

    def __init__(self, cast_device, http_streamer, server_ip: str, device_name: str = "Chromecast"):
        self._cast = cast_device
        self._streamer = http_streamer
        self._server_ip = server_ip
        self._name = device_name
        self._available = True

    @property
    def name(self) -> str:
        return self._name

    @property
    def available(self) -> bool:
        return self._available

    async def start(self, stream_info, track=None) -> None:
        """Start casting audio."""
        try:
            mc = self._cast.media_controller

            # Build stream URL
            if hasattr(stream_info, 'stream_id'):
                url = self._streamer.get_stream_url(stream_info.stream_id, self._server_ip)
            elif track and track.file_path and track.file_path.startswith("http"):
                url = track.file_path
            else:
                logger.warning("chromecast_no_url")
                return

            content_type = "audio/flac"
            if track and track.format:
                fmt = track.format.value if hasattr(track.format, 'value') else str(track.format)
                mime_map = {
                    "flac": "audio/flac", "mp3": "audio/mpeg", "aac": "audio/mp4",
                    "wav": "audio/wav", "ogg": "audio/ogg", "opus": "audio/opus",
                }
                content_type = mime_map.get(fmt, "audio/flac")

            title = track.title if track else "Audio"
            artist = track.artist_name if track else None

            await asyncio.to_thread(
                mc.play_media, url, content_type,
                title=title, artist=artist,
            )
            await asyncio.to_thread(mc.block_until_active, timeout=10)
            logger.info("chromecast_playing", device=self._name, title=title)

        except Exception:
            logger.exception("chromecast_start_error", device=self._name)
            self._available = False

    async def stop(self) -> None:
        try:
            mc = self._cast.media_controller
            await asyncio.to_thread(mc.stop)
        except Exception:
            pass

    async def pause(self) -> None:
        try:
            await asyncio.to_thread(self._cast.media_controller.pause)
        except Exception:
            pass

    async def resume(self) -> None:
        try:
            await asyncio.to_thread(self._cast.media_controller.play)
        except Exception:
            pass

    async def set_volume(self, volume: float) -> None:
        try:
            await asyncio.to_thread(self._cast.set_volume, volume)
        except Exception:
            pass

    async def seek(self, position_ms: int) -> bool:
        try:
            await asyncio.to_thread(self._cast.media_controller.seek, position_ms / 1000)
            return True
        except Exception:
            return False

    def supports_direct_url(self, track=None) -> bool:
        return True


async def discover_chromecasts(timeout: int = 5) -> list[dict]:
    """Discover Chromecast devices on the network."""
    try:
        import pychromecast
        casts, browser = await asyncio.to_thread(
            pychromecast.get_chromecasts, timeout=timeout
        )
        devices = []
        for cast in casts:
            devices.append({
                "id": str(cast.uuid),
                "name": cast.cast_info.friendly_name,
                "model": cast.cast_info.model_name,
                "host": str(cast.cast_info.host),
                "port": cast.cast_info.port,
                "type": "chromecast",
            })
        browser.stop_discovery()
        return devices
    except ImportError:
        logger.debug("pychromecast_not_installed")
        return []
    except Exception:
        logger.debug("chromecast_discovery_error", exc_info=True)
        return []

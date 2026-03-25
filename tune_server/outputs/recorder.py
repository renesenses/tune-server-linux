"""Recorder output — virtual zone that downloads tracks to disk."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Optional

import structlog

from tune_server.audio.formats import RECORDER_CAPABILITIES, AudioCapabilities
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import AudioFormat, AudioStreamInfo, Source, Track
from tune_server.outputs.base import OutputTarget
from tune_server.recording.stream_capture import copy_local_file, download_stream
from tune_server.recording.tagger import download_cover, tag_file

logger = structlog.get_logger()

# Sources that should NOT be recorded (DRM / ToS)
EXCLUDED_SOURCES = {Source.AMAZON, Source.SPOTIFY, Source.DEEZER}


class RecorderOutput(OutputTarget):
    """Virtual output that downloads tracks to disk instead of playing audio."""

    def __init__(
        self,
        event_bus: EventBus,
        output_dir: str | Path = "~/Music/Recordings",
    ) -> None:
        self._event_bus = event_bus
        self._output_dir = Path(output_dir).expanduser()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._available = True

        self._current_track: Optional[Track] = None
        self._download_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._download_complete = asyncio.Event()
        self._bytes_written = 0

    # ── OutputTarget interface ──────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "Recorder"

    @property
    def capabilities(self) -> AudioCapabilities:
        return RECORDER_CAPABILITIES

    @property
    def is_available(self) -> bool:
        return self._available

    def supports_direct_url(self, track: Track) -> bool:
        return True

    async def start(self, stream_info: AudioStreamInfo, track: Optional[Track] = None) -> None:
        """Start downloading a track."""
        # Cancel any in-progress download
        await self._cancel_download()

        if not track:
            return

        self._current_track = track
        self._download_complete.clear()
        self._stop_event.clear()
        self._bytes_written = 0

        # Enrich metadata from streaming service if missing
        await self._enrich_track(track)

        # Check excluded sources
        if track.source in EXCLUDED_SOURCES:
            logger.info("recorder_source_excluded", source=track.source, title=track.title)
            self._download_complete.set()
            return

        url = track.file_path
        if not url:
            logger.warning("recorder_no_url", title=track.title)
            self._download_complete.set()
            return

        output_path = self._build_output_path(track)
        self._download_task = asyncio.create_task(
            self._download_and_tag(track, url, output_path)
        )

    async def write(self, data: bytes) -> None:
        pass  # No audio pipeline — download is handled in start()

    async def flush(self) -> None:
        pass

    async def pause(self) -> None:
        pass  # Download continues in background

    async def resume(self) -> None:
        pass

    async def stop(self) -> None:
        await self._cancel_download()

    async def set_volume(self, volume: float) -> None:
        pass  # No audio output

    async def close(self) -> None:
        await self._cancel_download()

    async def get_position_ms(self) -> int:
        """Report download progress as position.

        Returns track duration when download is complete, triggering auto-advance.
        Returns -1 while downloading (player uses wall-clock as fallback).
        """
        if self._download_complete.is_set():
            duration = self._current_track.duration_ms if self._current_track else 0
            # Return duration + 1 to ensure the monitor advances
            return max(duration + 1, 1)
        return -1

    # ── Internal ────────────────────────────────────────────────────────────

    async def _download_and_tag(self, track: Track, url: str, output_path: Path) -> None:
        """Download the track, tag it, then signal completion."""
        try:
            source_name = track.source.value if track.source else "unknown"

            await self._event_bus.emit(Event(
                type=EventType.RECORDING_STARTED,
                data={"track_title": track.title, "source": source_name,
                      "path": str(output_path)},
                source="recorder_output",
            ))

            if url.startswith(("http://", "https://")):
                self._bytes_written = await download_stream(
                    url, output_path,
                    on_progress=self._on_progress,
                    stop_event=self._stop_event,
                )
            else:
                self._bytes_written = copy_local_file(url, output_path)

            # Tag the file with metadata
            cover_data = None
            if track.cover_path and track.cover_path.startswith("http"):
                cover_data = await download_cover(track.cover_path)

            await tag_file(
                output_path,
                title=track.title or "",
                artist=track.artist_name or "",
                album=track.album_title or "",
                track_number=track.track_number,
                cover_data=cover_data,
            )

            await self._event_bus.emit(Event(
                type=EventType.RECORDING_TRACK_SAVED,
                data={"track_title": track.title, "source": source_name,
                      "file_path": str(output_path), "bytes": self._bytes_written},
                source="recorder_output",
            ))

            logger.info("recorder_track_saved", title=track.title,
                        path=str(output_path), bytes=self._bytes_written)

        except asyncio.CancelledError:
            # Clean up partial file
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            raise
        except Exception:
            logger.exception("recorder_download_error", title=track.title)
            # Clean up corrupted/partial file
            if output_path.exists() and output_path.stat().st_size < 1024:
                output_path.unlink(missing_ok=True)
            await self._event_bus.emit(Event(
                type=EventType.RECORDING_ERROR,
                data={"track_title": track.title, "error": "Download failed"},
                source="recorder_output",
            ))
        finally:
            self._download_complete.set()

    async def _enrich_track(self, track: Track) -> None:
        """Fill missing album_title/cover/artist from the streaming connector."""
        if track.album_title and track.cover_path:
            return
        if not track.source_id or not track.source:
            return
        try:
            from tune_server.api.deps import deps
            svc_name = track.source.value if isinstance(track.source, Source) else str(track.source)
            svc = deps.streaming_services.get(svc_name)
            if svc:
                full = await svc.get_track(track.source_id)
                if full:
                    if not track.album_title and full.album_title:
                        track.album_title = full.album_title
                    if not track.cover_path and full.cover_path:
                        track.cover_path = full.cover_path
                    if not track.artist_name and full.artist_name:
                        track.artist_name = full.artist_name
                    if not track.track_number and full.track_number:
                        track.track_number = full.track_number
        except Exception:
            logger.debug("recorder_enrich_failed", source_id=track.source_id)

    def _on_progress(self, bytes_written: int) -> None:
        self._bytes_written = bytes_written

    async def _cancel_download(self) -> None:
        self._stop_event.set()
        if self._download_task and not self._download_task.done():
            self._download_task.cancel()
            try:
                await asyncio.wait_for(self._download_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self._download_task = None
        self._download_complete.set()

    def _build_output_path(self, track: Track) -> Path:
        source_name = track.source.value if track.source else "unknown"
        artist = _safe_dir(track.artist_name or "Unknown Artist")
        album = _safe_dir(track.album_title or "Unknown Album")
        title = _safe_dir(track.title or "Unknown Track")

        fmt = track.format.value if track.format else "flac"
        ext_map = {
            "flac": ".flac", "mp3": ".mp3", "aac": ".aac", "m4a": ".m4a",
            "opus": ".opus", "ogg": ".ogg", "wav": ".wav", "dsd": ".dsf",
            "alac": ".m4a", "aiff": ".aiff",
        }
        ext = ext_map.get(fmt, ".flac")

        # Include disc number for multi-disc albums
        disc = track.disc_number or 1
        prefix = ""
        if disc > 1:
            prefix += f"D{disc} "
        if track.track_number:
            prefix += f"{track.track_number:02d} - "

        # Avoid filename collisions: append source_id if file already exists
        base_path = self._output_dir / source_name.capitalize() / artist / album / f"{prefix}{title}{ext}"
        if base_path.exists() and track.source_id:
            base_path = base_path.with_stem(f"{prefix}{title} ({track.source_id})")
        return base_path


def _safe_dir(name: str, max_len: int = 80) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', '_', name).strip(". ")
    return safe[:max_len] if safe else "Unknown"

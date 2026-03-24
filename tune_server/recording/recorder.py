"""Main recording service — listens to playback events and captures audio."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import Source, Track
from tune_server.recording.models import RecordingSession, RecordingState
from tune_server.recording.stream_capture import (
    capture_radio_stream,
    copy_local_file,
    download_stream,
)
from tune_server.recording.tagger import download_cover, tag_file

logger = structlog.get_logger()

# Sources that should NOT be recorded (DRM or ToS)
EXCLUDED_SOURCES = {Source.AMAZON, Source.SPOTIFY, Source.DEEZER}


class RecordingService:
    """Manages stream recording per zone."""

    def __init__(
        self,
        event_bus: EventBus,
        output_dir: str | Path = "~/Music/Recordings",
    ) -> None:
        self._event_bus = event_bus
        self._output_dir = Path(output_dir).expanduser()
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Active recordings per zone
        self._sessions: dict[int, RecordingSession] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._stop_events: dict[int, asyncio.Event] = {}

        # Zones where recording is enabled
        self._enabled_zones: set[int] = set()

        # Track URLs captured at play time (before they expire)
        self._track_urls: dict[int, str] = {}  # zone_id -> url
        self._track_info: dict[int, Track] = {}  # zone_id -> track

        # Subscribe to events
        self._unsubs: list = []

    async def start(self) -> None:
        """Start listening for playback events."""
        self._unsubs.append(
            self._event_bus.on(EventType.PLAYBACK_STARTED, self._on_track_started)
        )
        self._unsubs.append(
            self._event_bus.on(EventType.PLAYBACK_TRACK_CHANGED, self._on_track_changed)
        )
        self._unsubs.append(
            self._event_bus.on(EventType.PLAYBACK_STOPPED, self._on_playback_stopped)
        )
        self._unsubs.append(
            self._event_bus.on(EventType.PLAYBACK_METADATA, self._on_metadata)
        )
        logger.info("recording_service_started", output_dir=str(self._output_dir))

    async def stop(self) -> None:
        """Stop all recordings and unsubscribe."""
        for zone_id in list(self._tasks.keys()):
            await self.stop_recording(zone_id)
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # ── Public API ─────────────────────────────────────────────────────────

    def start_recording_zone(self, zone_id: int) -> None:
        """Enable recording for a zone."""
        self._enabled_zones.add(zone_id)
        logger.info("recording_enabled", zone_id=zone_id)

    async def stop_recording(self, zone_id: int) -> Optional[RecordingSession]:
        """Stop recording for a zone and finalize."""
        self._enabled_zones.discard(zone_id)

        # Signal stop
        stop_event = self._stop_events.pop(zone_id, None)
        if stop_event:
            stop_event.set()

        # Wait for task to finish
        task = self._tasks.pop(zone_id, None)
        if task and not task.done():
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()

        session = self._sessions.pop(zone_id, None)
        if session:
            session.state = RecordingState.IDLE
            logger.info("recording_stopped", zone_id=zone_id,
                        file=str(session.file_path))

        return session

    def get_session(self, zone_id: int) -> Optional[RecordingSession]:
        """Get the current recording session for a zone."""
        return self._sessions.get(zone_id)

    def is_recording(self, zone_id: int) -> bool:
        return zone_id in self._sessions and self._sessions[zone_id].state == RecordingState.RECORDING

    def get_all_sessions(self) -> dict[int, RecordingSession]:
        return dict(self._sessions)

    def list_recordings(self) -> list[dict]:
        """List all recorded files."""
        recordings = []
        for path in sorted(self._output_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in (
                ".flac", ".mp3", ".aac", ".m4a", ".ogg", ".opus", ".wav", ".dsf"
            ):
                recordings.append({
                    "path": str(path.relative_to(self._output_dir)),
                    "size": path.stat().st_size,
                    "modified": path.stat().st_mtime,
                })
        return recordings

    # ── Event Handlers ─────────────────────────────────────────────────────

    async def _on_track_started(self, event: Event) -> None:
        zone_id = event.data.get("zone_id")
        if zone_id is None or zone_id not in self._enabled_zones:
            return
        await self._start_capture(zone_id)

    async def _on_track_changed(self, event: Event) -> None:
        zone_id = event.data.get("zone_id")
        if zone_id is None or zone_id not in self._enabled_zones:
            return
        # Finalize current, start new
        await self._finalize_current(zone_id)
        await self._start_capture(zone_id)

    async def _on_playback_stopped(self, event: Event) -> None:
        zone_id = event.data.get("zone_id")
        if zone_id is None:
            return
        await self._finalize_current(zone_id)

    async def _on_metadata(self, event: Event) -> None:
        """Handle radio ICY metadata updates (for radio recording file names)."""
        zone_id = event.data.get("zone_id")
        if zone_id is None or zone_id not in self._sessions:
            return
        session = self._sessions[zone_id]
        session.track_title = event.data.get("title", session.track_title)
        session.artist_name = event.data.get("artist_name", session.artist_name)

    # ── Internal ───────────────────────────────────────────────────────────

    def set_track_info(self, zone_id: int, track: Track) -> None:
        """Called by the player to capture track info before playback starts."""
        self._track_info[zone_id] = track
        if track.file_path:
            self._track_urls[zone_id] = track.file_path

    async def _start_capture(self, zone_id: int) -> None:
        """Start capturing the current track for a zone."""
        track = self._track_info.get(zone_id)
        url = self._track_urls.get(zone_id)

        if not track:
            logger.warning("recording_no_track_info", zone_id=zone_id)
            return

        # Check excluded sources
        if track.source in EXCLUDED_SOURCES:
            logger.info("recording_source_excluded", source=track.source, zone_id=zone_id)
            return

        # Cancel any existing capture for this zone
        await self._finalize_current(zone_id)

        # Determine output path
        source_name = track.source.value if track.source else "unknown"
        output_path = self._build_output_path(track, source_name)

        # Create session
        session = RecordingSession(
            zone_id=zone_id,
            state=RecordingState.RECORDING,
            track_title=track.title or "Unknown",
            artist_name=track.artist_name or "",
            album_title=track.album_title or "",
            source=source_name,
            source_id=track.source_id or "",
            format=track.format.value if track.format else "",
            file_path=output_path,
            cover_url=track.cover_path if track.cover_path and track.cover_path.startswith("http") else None,
        )
        self._sessions[zone_id] = session

        # Create stop event
        stop_event = asyncio.Event()
        self._stop_events[zone_id] = stop_event

        # Start capture task
        if track.source == Source.RADIO:
            task = asyncio.create_task(self._capture_radio(zone_id, track, stop_event))
        elif url and url.startswith(("http://", "https://")):
            task = asyncio.create_task(self._capture_stream(zone_id, track, url, output_path, stop_event))
        elif track.file_path and not track.file_path.startswith("http"):
            task = asyncio.create_task(self._capture_local(zone_id, track, output_path))
        else:
            logger.warning("recording_no_capture_method", zone_id=zone_id, track=track.title)
            session.state = RecordingState.ERROR
            session.error = "No capture method available"
            return

        self._tasks[zone_id] = task

        await self._event_bus.emit(Event(
            type=EventType.RECORDING_STARTED,
            data={"zone_id": zone_id, "track_title": track.title, "source": source_name},
            source="recorder",
        ))

        logger.info("recording_started", zone_id=zone_id, track=track.title,
                     source=source_name, path=str(output_path))

    async def _capture_stream(
        self, zone_id: int, track: Track, url: str, output_path: Path,
        stop_event: asyncio.Event,
    ) -> None:
        """Parallel download of a stream URL."""
        session = self._sessions.get(zone_id)
        try:
            bytes_written = await download_stream(
                url, output_path,
                on_progress=lambda b: setattr(session, "bytes_written", b) if session else None,
                stop_event=stop_event,
            )
            if session:
                session.bytes_written = bytes_written
                session.duration_ms = track.duration_ms or 0

            # Tag the file
            await self._tag_recording(track, output_path)

        except Exception as e:
            logger.exception("recording_capture_error", zone_id=zone_id)
            if session:
                session.state = RecordingState.ERROR
                session.error = str(e)

    async def _capture_radio(
        self, zone_id: int, track: Track, stop_event: asyncio.Event,
    ) -> None:
        """Capture a radio stream with ICY splitting."""
        url = track.file_path
        if not url:
            return

        station_name = track.title or "Radio"
        date_str = datetime.now().strftime("%Y-%m-%d")
        output_dir = self._output_dir / "Radio" / _safe_dir(station_name) / date_str

        session = self._sessions.get(zone_id)
        try:
            files = await capture_radio_stream(
                url, output_dir, stop_event=stop_event,
            )
            if session:
                session.bytes_written = sum(f.stat().st_size for f in files if f.exists())
                session.file_path = output_dir

            logger.info("radio_recording_complete", tracks=len(files), dir=str(output_dir))

        except Exception as e:
            logger.exception("radio_recording_error", zone_id=zone_id)
            if session:
                session.state = RecordingState.ERROR
                session.error = str(e)

    async def _capture_local(
        self, zone_id: int, track: Track, output_path: Path,
    ) -> None:
        """Copy a local file."""
        session = self._sessions.get(zone_id)
        try:
            bytes_written = copy_local_file(track.file_path, output_path)
            if session:
                session.bytes_written = bytes_written
        except Exception as e:
            logger.exception("recording_copy_error", zone_id=zone_id)
            if session:
                session.state = RecordingState.ERROR
                session.error = str(e)

    async def _finalize_current(self, zone_id: int) -> None:
        """Finalize the current recording for a zone."""
        stop_event = self._stop_events.pop(zone_id, None)
        if stop_event:
            stop_event.set()

        task = self._tasks.pop(zone_id, None)
        if task and not task.done():
            try:
                await asyncio.wait_for(task, timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()

        session = self._sessions.pop(zone_id, None)
        if session and session.state == RecordingState.RECORDING:
            session.state = RecordingState.FINALIZING

            await self._event_bus.emit(Event(
                type=EventType.RECORDING_TRACK_SAVED,
                data={
                    "zone_id": zone_id,
                    "track_title": session.track_title,
                    "file_path": str(session.file_path),
                    "bytes": session.bytes_written,
                },
                source="recorder",
            ))

    async def _tag_recording(self, track: Track, file_path: Path) -> None:
        """Add metadata tags and cover art to recorded file."""
        cover_data = None
        if track.cover_path and track.cover_path.startswith("http"):
            cover_data = await download_cover(track.cover_path)

        await tag_file(
            file_path,
            title=track.title or "",
            artist=track.artist_name or "",
            album=track.album_title or "",
            track_number=track.track_number,
            year=track.year if hasattr(track, "year") else None,
            cover_data=cover_data,
        )

    def _build_output_path(self, track: Track, source_name: str) -> Path:
        """Build the output file path based on track metadata."""
        artist = _safe_dir(track.artist_name or "Unknown Artist")
        album = _safe_dir(track.album_title or "Unknown Album")
        title = _safe_dir(track.title or "Unknown Track")

        # Determine extension from format
        fmt = track.format.value if track.format else "flac"
        ext_map = {
            "flac": ".flac", "mp3": ".mp3", "aac": ".aac", "m4a": ".m4a",
            "opus": ".opus", "ogg": ".ogg", "wav": ".wav", "dsd": ".dsf",
            "alac": ".m4a", "aiff": ".aiff",
        }
        ext = ext_map.get(fmt, ".flac")

        # Track number prefix
        prefix = f"{track.track_number:02d} - " if track.track_number else ""

        return self._output_dir / source_name.capitalize() / artist / album / f"{prefix}{title}{ext}"


def _safe_dir(name: str, max_len: int = 80) -> str:
    import re
    safe = re.sub(r'[<>:"/\\|?*]', '_', name).strip(". ")
    return safe[:max_len] if safe else "Unknown"

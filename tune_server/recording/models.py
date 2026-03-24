"""Recording data models."""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class RecordingState(str, enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    ERROR = "error"


@dataclass
class RecordingSession:
    """Represents an active or completed recording."""
    zone_id: int
    state: RecordingState = RecordingState.IDLE
    track_title: str = ""
    artist_name: str = ""
    album_title: str = ""
    source: str = ""
    source_id: str = ""
    format: str = ""
    file_path: Optional[Path] = None
    cover_url: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    bytes_written: int = 0
    duration_ms: int = 0
    error: Optional[str] = None

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started_at

    def to_dict(self) -> dict:
        return {
            "zone_id": self.zone_id,
            "state": self.state.value,
            "track_title": self.track_title,
            "artist_name": self.artist_name,
            "album_title": self.album_title,
            "source": self.source,
            "format": self.format,
            "file_path": str(self.file_path) if self.file_path else None,
            "bytes_written": self.bytes_written,
            "duration_ms": self.duration_ms,
            "elapsed_s": round(self.elapsed_s, 1),
            "error": self.error,
        }

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from tune_server.audio.formats import AudioCapabilities
from tune_server.models import AudioStreamInfo, Track


class OutputTarget(ABC):
    """Abstract base class for all audio output targets."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def capabilities(self) -> AudioCapabilities:
        ...

    @property
    @abstractmethod
    def is_available(self) -> bool:
        ...

    @abstractmethod
    async def start(self, stream_info: AudioStreamInfo, track: Optional[Track] = None) -> None:
        """Prepare the output for receiving audio data."""
        ...

    @abstractmethod
    async def write(self, data: bytes) -> None:
        """Write audio data to the output."""
        ...

    @abstractmethod
    async def flush(self) -> None:
        """Flush any buffered data."""
        ...

    @abstractmethod
    async def pause(self) -> None:
        ...

    @abstractmethod
    async def resume(self) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop playback."""
        ...

    @abstractmethod
    async def set_volume(self, volume: float) -> None:
        """Set volume (0.0 to 1.0)."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources."""
        ...

    def supports_direct_url(self, track: Track) -> bool:
        """Whether this output can fetch audio directly from a URL, skipping the local pipeline."""
        return False

    async def set_next_track(self, stream_info: AudioStreamInfo, track: Track) -> bool:
        """For gapless: set the next track (DLNA SetNextAVTransportURI). Returns True if supported."""
        return False

    async def get_position_ms(self) -> int:
        """Get current playback position. Returns -1 if not supported."""
        return -1

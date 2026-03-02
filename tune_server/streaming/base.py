from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

from tune_server.models import Album, Artist, FeaturedSection, SearchResult, StreamingPlaylist, Track

if TYPE_CHECKING:
    from tune_server.db.engine import Database


class StreamingService(ABC):
    """Abstract base class for streaming service integrations."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def is_authenticated(self) -> bool:
        ...

    @abstractmethod
    async def authenticate(self, **kwargs) -> bool:
        ...

    @abstractmethod
    async def search(self, query: str, limit: int = 50) -> SearchResult:
        ...

    @abstractmethod
    async def get_track(self, track_id: str) -> Optional[Track]:
        ...

    @abstractmethod
    async def get_album(self, album_id: str) -> Optional[Album]:
        ...

    @abstractmethod
    async def get_album_tracks(self, album_id: str) -> list[Track]:
        ...

    @abstractmethod
    async def get_artist(self, artist_id: str) -> Optional[Artist]:
        ...

    @abstractmethod
    async def get_stream_url(self, track_id: str) -> Optional[str]:
        ...

    @abstractmethod
    async def get_artist_albums(self, artist_id: str) -> list[Album]:
        ...

    @abstractmethod
    async def get_artist_tracks(self, artist_id: str) -> list[Track]:
        ...

    @property
    def verification_url(self) -> str | None:
        """Return pending OAuth verification URL, if any."""
        return None

    async def close(self) -> None:
        """Clean up resources. Override in subclasses."""

    async def save_auth(self, db: Database) -> None:
        """Persist auth tokens to DB. Override in subclasses."""

    async def restore_auth(self, db: Database) -> bool:
        """Restore auth tokens from DB. Override in subclasses. Returns True if restored."""
        return False

    async def disconnect(self, db: Database) -> None:
        """Clear auth tokens and remove from DB. Override in subclasses."""

    async def get_featured_sections(self) -> list[FeaturedSection]:
        """Return available featured content sections. Override in subclasses."""
        return []

    async def get_featured(self, section: str, limit: int = 20) -> list[Album]:
        """Return featured albums for a given section. Override in subclasses."""
        return []

    async def get_user_playlists(self) -> list[StreamingPlaylist]:
        """Return user's playlists. Override in subclasses."""
        return []

    async def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        """Return tracks for a playlist. Override in subclasses."""
        return []

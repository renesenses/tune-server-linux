from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

import aiohttp
import structlog

from tune_server.models import Album, Artist, FeaturedSection, SearchResult, StreamingPlaylist, Track

if TYPE_CHECKING:
    from tune_server.db.engine import Database

logger = structlog.get_logger()

# Exponential backoff delays for transient HTTP failures (seconds)
_HTTP_RETRY_DELAYS = (1, 3, 8)

# HTTP status codes that should NOT be retried (auth failures)
_NO_RETRY_STATUSES = {401, 403}

# HTTP status codes considered transient (server-side)
_TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}


async def http_request_with_retry(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    service_name: str = "",
    **kwargs,
) -> aiohttp.ClientResponse:
    """Execute an HTTP request with retry and exponential backoff.

    Retries on network timeouts, connection errors, and transient HTTP statuses
    (408, 429, 500, 502, 503, 504).  Does NOT retry on auth failures (401, 403).

    Returns the successful response.  Raises the last exception on exhaustion.
    """
    max_attempts = len(_HTTP_RETRY_DELAYS) + 1
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            resp = await session.request(method, url, **kwargs)

            if resp.status in _NO_RETRY_STATUSES:
                # Auth failures — raise immediately, no retry
                resp.raise_for_status()

            if resp.status in _TRANSIENT_STATUSES and attempt < max_attempts:
                delay = _HTTP_RETRY_DELAYS[attempt - 1]
                logger.warning(
                    "streaming_http_transient",
                    service=service_name,
                    url=url,
                    status=resp.status,
                    attempt=attempt,
                    next_retry_in=delay,
                )
                resp.release()
                await asyncio.sleep(delay)
                continue

            return resp

        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as exc:
            last_exc = exc
            if attempt < max_attempts:
                delay = _HTTP_RETRY_DELAYS[attempt - 1]
                logger.warning(
                    "streaming_http_timeout",
                    service=service_name,
                    url=url,
                    attempt=attempt,
                    next_retry_in=delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    "streaming_http_timeout_exhausted",
                    service=service_name,
                    url=url,
                    attempts=max_attempts,
                )
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ServerDisconnectedError,
            ConnectionRefusedError,
            ConnectionResetError,
            OSError,
        ) as exc:
            last_exc = exc
            if attempt < max_attempts:
                delay = _HTTP_RETRY_DELAYS[attempt - 1]
                logger.warning(
                    "streaming_http_connection_error",
                    service=service_name,
                    url=url,
                    attempt=attempt,
                    next_retry_in=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    "streaming_http_connection_exhausted",
                    service=service_name,
                    url=url,
                    attempts=max_attempts,
                    error=str(exc),
                )
        except aiohttp.ClientResponseError:
            # Non-transient HTTP errors (including 401/403 raised above)
            raise

    # All retries exhausted — raise the last exception
    if last_exc:
        raise last_exc
    raise aiohttp.ClientError(f"Request failed after {max_attempts} attempts: {url}")


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

    # ------------------------------------------------------------------
    # Playlist write operations (optional — override in subclasses)
    # ------------------------------------------------------------------

    @property
    def supports_playlist_write(self) -> bool:
        """Whether this service supports creating/modifying playlists."""
        return False

    async def create_playlist(self, name: str, description: str | None = None) -> str | None:
        """Create a playlist on the service. Returns the playlist ID or None."""
        return None

    async def add_tracks_to_playlist(self, playlist_id: str, track_ids: list[str]) -> int:
        """Add tracks to a playlist. Returns the number successfully added."""
        return 0

    async def remove_tracks_from_playlist(self, playlist_id: str, track_ids: list[str]) -> int:
        """Remove tracks from a playlist. Returns the number removed."""
        return 0

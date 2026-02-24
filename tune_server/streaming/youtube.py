from __future__ import annotations

import asyncio
import json
import os
import re
from typing import TYPE_CHECKING, Optional

import structlog

from tune_server.config import settings
from tune_server.models import Album, Artist, AudioFormat, SearchResult, Source, Track
from tune_server.streaming.base import StreamingService
from tune_server.streaming.cache import StreamUrlCache

if TYPE_CHECKING:
    from tune_server.db.engine import Database

logger = structlog.get_logger()

# Timeout for yt-dlp URL extraction
YTDLP_TIMEOUT = 30

# YouTube video IDs are 11 chars: alphanumeric, hyphens, underscores
_YT_VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")


class YouTubeService(StreamingService):
    """YouTube Music streaming service integration using ytmusicapi and yt-dlp."""

    def __init__(self) -> None:
        self._ytmusic = None
        self._oauth_json_path: str | None = settings.youtube_oauth_json
        self._url_cache = StreamUrlCache(ttl_seconds=settings.youtube_url_cache_ttl)
        self._lock = asyncio.Lock()  # Serialize access to _ytmusic (not thread-safe)

    @property
    def name(self) -> str:
        return "youtube"

    @property
    def is_authenticated(self) -> bool:
        return self._ytmusic is not None

    async def authenticate(self, **kwargs) -> bool:
        try:
            from ytmusicapi import YTMusic

            oauth_json = kwargs.get("oauth_json") or self._oauth_json_path

            async with self._lock:
                if oauth_json:
                    # Load from existing OAuth JSON file
                    self._ytmusic = await asyncio.to_thread(YTMusic, oauth_json)
                    self._oauth_json_path = oauth_json
                    logger.info("youtube_authenticated", method="oauth_file")
                    return True

                # Interactive OAuth device flow — generates oauth.json
                logger.info("youtube_auth_starting_oauth_flow")
                oauth_path = await asyncio.to_thread(YTMusic.setup_oauth, filepath="youtube_oauth.json")
                self._ytmusic = await asyncio.to_thread(YTMusic, oauth_path)
                self._oauth_json_path = oauth_path
                logger.info("youtube_authenticated", method="oauth_setup")
                return True

        except ImportError:
            logger.warning("ytmusicapi_not_installed")
            return False
        except Exception:
            logger.exception("youtube_auth_error")
            return False

    async def search(self, query: str, limit: int = 50) -> SearchResult:
        if not self._ytmusic:
            return SearchResult()

        try:
            async with self._lock:
                results = await asyncio.to_thread(self._ytmusic.search, query, limit=limit)

            tracks = []
            albums = []
            artists = []

            for item in results:
                category = item.get("resultType", item.get("category", ""))

                if category in ("song", "video") and len(tracks) < limit:
                    tracks.append(self._map_track_from_search(item))
                elif category == "album" and len(albums) < limit:
                    albums.append(self._map_album_from_search(item))
                elif category == "artist" and len(artists) < limit:
                    artists.append(self._map_artist_from_search(item))

            return SearchResult(tracks=tracks, albums=albums, artists=artists)

        except Exception:
            logger.exception("youtube_search_error")
            return SearchResult()

    async def get_track(self, track_id: str) -> Optional[Track]:
        if not self._ytmusic:
            return None
        try:
            async with self._lock:
                song = await asyncio.to_thread(self._ytmusic.get_song, track_id)
            return self._map_track_from_song(song)
        except Exception:
            logger.exception("youtube_get_track_error", track_id=track_id)
            return None

    async def get_album(self, album_id: str) -> Optional[Album]:
        if not self._ytmusic:
            return None
        try:
            async with self._lock:
                album = await asyncio.to_thread(self._ytmusic.get_album, album_id)
            return self._map_album_from_detail(album, album_id)
        except Exception:
            logger.exception("youtube_get_album_error", album_id=album_id)
            return None

    async def get_album_tracks(self, album_id: str) -> list[Track]:
        if not self._ytmusic:
            return []
        try:
            async with self._lock:
                album = await asyncio.to_thread(self._ytmusic.get_album, album_id)
            tracks = []
            for i, t in enumerate(album.get("tracks", []), start=1):
                tracks.append(Track(
                    title=t.get("title", "Unknown"),
                    artist_name=_first_artist_name(t.get("artists", [])),
                    album_title=album.get("title"),
                    track_number=i,
                    duration_ms=_parse_duration(t.get("duration", "0:00")),
                    format=AudioFormat.OPUS,
                    sample_rate=48000,
                    bit_depth=16,
                    channels=2,
                    source=Source.YOUTUBE,
                    source_id=t.get("videoId", ""),
                ))
            return tracks
        except Exception:
            logger.exception("youtube_album_tracks_error", album_id=album_id)
            return []

    async def get_artist(self, artist_id: str) -> Optional[Artist]:
        if not self._ytmusic:
            return None
        try:
            async with self._lock:
                ar = await asyncio.to_thread(self._ytmusic.get_artist, artist_id)
            return Artist(
                name=ar.get("name", "Unknown"),
            )
        except Exception:
            logger.exception("youtube_get_artist_error", artist_id=artist_id)
            return None

    async def get_artist_albums(self, artist_id: str) -> list[Album]:
        if not self._ytmusic:
            return []
        try:
            async with self._lock:
                ar = await asyncio.to_thread(self._ytmusic.get_artist, artist_id)
            albums_section = ar.get("albums", {})
            results = albums_section.get("results", []) if isinstance(albums_section, dict) else []
            return [
                Album(
                    title=a.get("title", "Unknown"),
                    artist_name=ar.get("name", "Unknown"),
                    year=_safe_int(a.get("year")),
                    source=Source.YOUTUBE,
                    source_id=a.get("browseId", ""),
                )
                for a in results
            ]
        except Exception:
            logger.exception("youtube_artist_albums_error", artist_id=artist_id)
            return []

    async def get_artist_tracks(self, artist_id: str) -> list[Track]:
        if not self._ytmusic:
            return []
        try:
            async with self._lock:
                ar = await asyncio.to_thread(self._ytmusic.get_artist, artist_id)
            songs_section = ar.get("songs", {})
            results = songs_section.get("results", []) if isinstance(songs_section, dict) else []
            return [
                Track(
                    title=t.get("title", "Unknown"),
                    artist_name=ar.get("name", "Unknown"),
                    duration_ms=_parse_duration(t.get("duration", "0:00")),
                    format=AudioFormat.OPUS,
                    sample_rate=48000,
                    bit_depth=16,
                    channels=2,
                    source=Source.YOUTUBE,
                    source_id=t.get("videoId", ""),
                )
                for t in results
            ]
        except Exception:
            logger.exception("youtube_artist_tracks_error", artist_id=artist_id)
            return []

    async def get_stream_url(self, track_id: str) -> Optional[str]:
        cached = self._url_cache.get(track_id)
        if cached:
            return cached

        try:
            url = await asyncio.wait_for(
                asyncio.to_thread(self._extract_url, track_id),
                timeout=YTDLP_TIMEOUT,
            )
            if url:
                self._url_cache.set(track_id, url)
            return url
        except asyncio.TimeoutError:
            logger.warning("youtube_stream_url_timeout", track_id=track_id)
            return None
        except Exception:
            logger.exception("youtube_stream_url_error", track_id=track_id)
            return None

    @staticmethod
    def _extract_url(track_id: str) -> str | None:
        """Extract audio URL using yt-dlp (runs in thread)."""
        if not _YT_VIDEO_ID_RE.match(track_id):
            logger.warning("youtube_invalid_track_id", track_id=track_id)
            return None

        from yt_dlp import YoutubeDL

        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
        }

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                f"https://music.youtube.com/watch?v={track_id}",
                download=False,
            )
            return info.get("url") if info else None

    async def save_auth(self, db: Database) -> None:
        if not self._oauth_json_path:
            return
        try:
            token_data = json.dumps({"oauth_json_path": self._oauth_json_path})
            await db.execute(
                "INSERT OR REPLACE INTO streaming_auth (service, token_data, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                ("youtube", token_data),
            )
            await db.commit()
            logger.info("youtube_auth_saved")
        except Exception:
            logger.exception("youtube_save_auth_error")

    async def restore_auth(self, db: Database) -> bool:
        try:
            from ytmusicapi import YTMusic

            row = await db.fetchone(
                "SELECT token_data FROM streaming_auth WHERE service = ?", ("youtube",)
            )
            if not row:
                return False

            data = json.loads(row["token_data"])
            oauth_path = data.get("oauth_json_path")
            if not oauth_path:
                return False

            if not os.path.isfile(oauth_path):
                logger.warning("youtube_oauth_file_not_found", path=oauth_path)
                return False

            async with self._lock:
                self._ytmusic = await asyncio.to_thread(YTMusic, oauth_path)
            self._oauth_json_path = oauth_path
            logger.info("youtube_auth_restored")
            return True

        except ImportError:
            logger.warning("ytmusicapi_not_installed")
            return False
        except Exception:
            logger.exception("youtube_restore_auth_error")
            return False

    async def close(self) -> None:
        async with self._lock:
            self._ytmusic = None
        self._url_cache.clear()

    # --- Mapping helpers ---

    def _map_track_from_search(self, item: dict) -> Track:
        artists = item.get("artists", [])
        album = item.get("album", {}) or {}
        return Track(
            title=item.get("title", "Unknown"),
            artist_name=_first_artist_name(artists),
            album_title=album.get("name") if isinstance(album, dict) else None,
            duration_ms=_parse_duration(item.get("duration", "0:00")),
            format=AudioFormat.OPUS,
            sample_rate=48000,
            bit_depth=16,
            channels=2,
            source=Source.YOUTUBE,
            source_id=item.get("videoId", ""),
        )

    def _map_track_from_song(self, song: dict) -> Track:
        details = song.get("videoDetails", {})
        return Track(
            title=details.get("title", "Unknown"),
            artist_name=details.get("author", "Unknown"),
            duration_ms=int(details.get("lengthSeconds", 0)) * 1000,
            format=AudioFormat.OPUS,
            sample_rate=48000,
            bit_depth=16,
            channels=2,
            source=Source.YOUTUBE,
            source_id=details.get("videoId", ""),
        )

    def _map_album_from_search(self, item: dict) -> Album:
        artists = item.get("artists", [])
        return Album(
            title=item.get("title", "Unknown"),
            artist_name=_first_artist_name(artists),
            year=_safe_int(item.get("year")),
            source=Source.YOUTUBE,
            source_id=item.get("browseId", ""),
        )

    def _map_album_from_detail(self, album: dict, album_id: str) -> Album:
        artists = album.get("artists", [])
        return Album(
            title=album.get("title", "Unknown"),
            artist_name=_first_artist_name(artists),
            year=_safe_int(album.get("year")),
            track_count=len(album.get("tracks", [])),
            source=Source.YOUTUBE,
            source_id=album_id,
        )

    def _map_artist_from_search(self, item: dict) -> Artist:
        return Artist(
            name=item.get("artist", item.get("title", "Unknown")),
        )


def _first_artist_name(artists: list) -> str:
    if not artists:
        return "Unknown"
    first = artists[0]
    if isinstance(first, dict):
        return first.get("name", "Unknown")
    if isinstance(first, str):
        return first
    return "Unknown"


def _parse_duration(duration_str: str) -> int:
    """Parse 'M:SS' or 'H:MM:SS' to milliseconds."""
    try:
        parts = duration_str.split(":")
        if len(parts) == 2:
            return (int(parts[0]) * 60 + int(parts[1])) * 1000
        elif len(parts) == 3:
            return (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
    except (ValueError, AttributeError):
        pass
    return 0


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None

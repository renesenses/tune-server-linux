from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Optional

import structlog

from tune_server.config import settings
from tune_server.models import (
    Album,
    Artist,
    AudioFormat,
    FeaturedSection,
    SearchResult,
    Source,
    StreamingPlaylist,
    Track,
)
from tune_server.streaming.base import StreamingService
from tune_server.streaming.cache import StreamUrlCache

if TYPE_CHECKING:
    from tune_server.db.engine import Database

logger = structlog.get_logger()

# Timeout for yt-dlp URL extraction
YTDLP_TIMEOUT = 30

# YouTube video IDs are 11 chars: alphanumeric, hyphens, underscores
_YT_VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")

# Google/YouTube OAuth endpoints (non-standard grant type used by ytmusicapi)
OAUTH_CODE_URL = "https://www.youtube.com/o/oauth2/device/code"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube"
DEVICE_GRANT_TYPE = "http://oauth.net/grant_type/device/1.0"


class YouTubeService(StreamingService):
    """YouTube Music streaming service integration using ytmusicapi and yt-dlp."""

    def __init__(self) -> None:
        self._ytmusic = None
        self._oauth_credentials = None  # ytmusicapi OAuthCredentials
        self._token_data: dict | None = None
        self._verification_url: str | None = None
        self._user_code: str | None = None
        self._url_cache = StreamUrlCache(ttl_seconds=settings.youtube_url_cache_ttl)
        self._lock = asyncio.Lock()  # Serialize access to _ytmusic (not thread-safe)
        self._featured_cache: dict[str, list] = {}
        self._poll_task: asyncio.Task | None = None

    @property
    def name(self) -> str:
        return "youtube"

    @property
    def is_authenticated(self) -> bool:
        return self._ytmusic is not None

    @property
    def verification_url(self) -> str | None:
        return self._verification_url

    @property
    def user_code(self) -> str | None:
        return self._user_code

    async def authenticate(self, **kwargs) -> bool:
        db = kwargs.get("db")

        # Legacy: load from oauth.json file path
        oauth_json = kwargs.get("oauth_json") or settings.youtube_oauth_json
        if oauth_json:
            return await self._auth_from_file(oauth_json, db)

        # Device code OAuth flow (requires client_id + client_secret)
        if not settings.youtube_client_id or not settings.youtube_client_secret:
            logger.warning("youtube_auth_no_credentials",
                           hint="Set TUNE_YOUTUBE_CLIENT_ID and TUNE_YOUTUBE_CLIENT_SECRET")
            return False

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.post(OAUTH_CODE_URL, data={
                    "client_id": settings.youtube_client_id,
                    "scope": OAUTH_SCOPE,
                }) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning("youtube_device_code_error", status=resp.status, body=text)
                        return False
                    code_data = await resp.json()

            device_code = code_data.get("device_code")
            user_code = code_data.get("user_code")
            verification_url = code_data.get("verification_url", "https://www.google.com/device")
            interval = code_data.get("interval", 5)

            self._verification_url = verification_url
            self._user_code = user_code

            logger.info("youtube_auth_started",
                        verification_url=verification_url,
                        user_code=user_code)

            # Start background polling for token
            self._poll_task = asyncio.create_task(
                self._poll_device_auth(device_code, interval, db)
            )

            return False  # waiting for user authorization

        except ImportError:
            logger.warning("aiohttp_not_installed")
            return False
        except Exception:
            logger.exception("youtube_auth_error")
            return False

    async def _auth_from_file(self, oauth_json: str, db: Database | None = None) -> bool:
        """Legacy auth: load from an existing oauth.json file."""
        try:
            from ytmusicapi import YTMusic

            async with self._lock:
                self._ytmusic = await asyncio.to_thread(YTMusic, oauth_json)
            logger.info("youtube_authenticated", method="oauth_file")

            if db:
                # Read file content and store in DB for portability
                with open(oauth_json) as f:
                    self._token_data = json.load(f)
                await self.save_auth(db)

            return True
        except ImportError:
            logger.warning("ytmusicapi_not_installed")
            return False
        except Exception:
            logger.exception("youtube_auth_file_error")
            return False

    async def _poll_device_auth(self, device_code: str, interval: int, db: Database | None) -> None:
        """Background task: poll Google token endpoint until user authorizes."""
        try:
            import aiohttp

            poll_interval = interval
            max_attempts = 360  # 30 min at 5s intervals

            for _ in range(max_attempts):
                await asyncio.sleep(poll_interval)

                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(OAUTH_TOKEN_URL, data={
                            "client_id": settings.youtube_client_id,
                            "client_secret": settings.youtube_client_secret,
                            "code": device_code,
                            "grant_type": DEVICE_GRANT_TYPE,
                        }) as resp:
                            data = await resp.json()
                except Exception:
                    logger.exception("youtube_poll_request_error")
                    continue

                if "access_token" in data:
                    # Success!
                    token_data = {
                        "scope": data.get("scope", OAUTH_SCOPE),
                        "token_type": data.get("token_type", "Bearer"),
                        "access_token": data["access_token"],
                        "refresh_token": data["refresh_token"],
                        "expires_at": int(time.time()) + data.get("expires_in", 3600),
                        "expires_in": data.get("expires_in", 3600),
                    }
                    self._token_data = token_data
                    await self._init_client_from_tokens(token_data)

                    self._verification_url = None
                    self._user_code = None

                    logger.info("youtube_authenticated", method="device_code")

                    if db:
                        await self.save_auth(db)
                    return

                error = data.get("error")
                if error == "authorization_pending":
                    continue
                elif error == "slow_down":
                    poll_interval += 5
                    continue
                else:
                    # access_denied, expired_token, or other error
                    logger.warning("youtube_device_auth_failed", error=error)
                    self._verification_url = None
                    self._user_code = None
                    return

            # Timeout
            logger.warning("youtube_device_auth_timeout")
            self._verification_url = None
            self._user_code = None

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("youtube_poll_error")
            self._verification_url = None
            self._user_code = None

    async def _init_client_from_tokens(self, token_data: dict) -> None:
        """Create YTMusic instance from token dict + OAuthCredentials."""
        from ytmusicapi import YTMusic
        from ytmusicapi.auth.oauth.credentials import OAuthCredentials

        self._oauth_credentials = OAuthCredentials(
            client_id=settings.youtube_client_id,
            client_secret=settings.youtube_client_secret,
        )

        async with self._lock:
            self._ytmusic = await asyncio.to_thread(
                YTMusic,
                auth=token_data,
                oauth_credentials=self._oauth_credentials,
            )

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
            cover = _best_thumbnail(album.get("thumbnails", []))
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
                    cover_path=cover,
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
            image_path = _best_thumbnail(ar.get("thumbnails", []))
            return Artist(
                name=ar.get("name", "Unknown"),
                image_path=image_path,
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
                    cover_path=_best_thumbnail(a.get("thumbnails", [])),
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
                    cover_path=_best_thumbnail(t.get("thumbnails", [])),
                    source=Source.YOUTUBE,
                    source_id=t.get("videoId", ""),
                )
                for t in results
            ]
        except Exception:
            logger.exception("youtube_artist_tracks_error", artist_id=artist_id)
            return []

    async def get_featured_sections(self) -> list[FeaturedSection]:
        if not self._ytmusic:
            return []
        try:
            async with self._lock:
                home = await asyncio.to_thread(self._ytmusic.get_home, limit=6)
            self._featured_cache = {}
            sections = []
            for i, row in enumerate(home):
                title = row.get("title", "")
                if not title:
                    continue
                items = row.get("contents", [])
                has_albums = any(
                    item.get("resultType") in ("album", "single")
                    or item.get("browseId", "").startswith("MPREb_")
                    for item in items
                )
                if has_albums:
                    section_id = f"home-{i}"
                    sections.append(FeaturedSection(id=section_id, name=title))
                    self._featured_cache[section_id] = items
            return sections
        except Exception:
            logger.exception("youtube_featured_sections_error")
            return []

    async def get_featured(self, section: str, limit: int = 20) -> list[Album]:
        items = self._featured_cache.get(section, [])
        albums = []
        for item in items[:limit]:
            browse_id = item.get("browseId", "")
            if not browse_id:
                continue
            cover = _best_thumbnail(item.get("thumbnails", []))
            albums.append(Album(
                title=item.get("title", ""),
                artist_name=", ".join(
                    a.get("name", "") for a in item.get("artists", [])
                ) or None,
                year=_safe_int(item.get("year")),
                cover_path=cover,
                source=Source.YOUTUBE,
                source_id=browse_id,
            ))
        return albums

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

    async def get_user_playlists(self) -> list[StreamingPlaylist]:
        if not self._ytmusic:
            return []
        try:
            async with self._lock:
                raw = await asyncio.to_thread(self._ytmusic.get_library_playlists, limit=50)
            return [
                StreamingPlaylist(
                    source_id=p.get("playlistId", ""),
                    name=p.get("title", "Unknown"),
                    description=p.get("description"),
                    track_count=_safe_int(p.get("count")) or 0,
                    duration_ms=0,
                    cover_path=_best_thumbnail(p.get("thumbnails", [])),
                    source=Source.YOUTUBE,
                )
                for p in raw
                if p.get("playlistId")
            ]
        except Exception:
            logger.exception("youtube_user_playlists_error")
            return []

    async def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        if not self._ytmusic:
            return []
        try:
            async with self._lock:
                playlist = await asyncio.to_thread(
                    self._ytmusic.get_playlist, playlist_id, limit=500
                )
            tracks = []
            for t in playlist.get("tracks", []):
                video_id = t.get("videoId")
                if not video_id:
                    continue
                tracks.append(Track(
                    title=t.get("title", "Unknown"),
                    artist_name=_first_artist_name(t.get("artists", [])),
                    album_title=(t.get("album") or {}).get("name") if isinstance(t.get("album"), dict) else None,
                    duration_ms=_parse_duration(t.get("duration", "0:00")),
                    format=AudioFormat.OPUS,
                    sample_rate=48000,
                    bit_depth=16,
                    channels=2,
                    cover_path=_best_thumbnail(t.get("thumbnails", [])),
                    source=Source.YOUTUBE,
                    source_id=video_id,
                ))
            return tracks
        except Exception:
            logger.exception("youtube_playlist_tracks_error", playlist_id=playlist_id)
            return []

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
        if not self._token_data:
            return
        try:
            save_data = {**self._token_data}
            # Include client credentials so restore can recreate OAuthCredentials
            if settings.youtube_client_id:
                save_data["client_id"] = settings.youtube_client_id
                save_data["client_secret"] = settings.youtube_client_secret
            token_json = json.dumps(save_data)
            await db.execute(
                "INSERT OR REPLACE INTO streaming_auth (service, token_data, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                ("youtube", token_json),
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
                # Fallback: legacy file-based auth
                return await self._restore_from_file()

            data = json.loads(row["token_data"])

            # Legacy format: just a file path reference
            if "oauth_json_path" in data and "access_token" not in data:
                return await self._restore_from_file(data.get("oauth_json_path"))

            # Token-based auth
            if "access_token" not in data:
                return False

            client_id = data.pop("client_id", None) or settings.youtube_client_id
            client_secret = data.pop("client_secret", None) or settings.youtube_client_secret

            if not client_id or not client_secret:
                logger.warning("youtube_restore_no_credentials")
                return False

            self._token_data = data

            from ytmusicapi.auth.oauth.credentials import OAuthCredentials
            self._oauth_credentials = OAuthCredentials(
                client_id=client_id,
                client_secret=client_secret,
            )

            async with self._lock:
                self._ytmusic = await asyncio.to_thread(
                    YTMusic,
                    auth=data,
                    oauth_credentials=self._oauth_credentials,
                )

            logger.info("youtube_auth_restored", method="tokens")
            return True

        except ImportError:
            logger.warning("ytmusicapi_not_installed")
            return False
        except Exception:
            logger.exception("youtube_restore_auth_error")
            self._ytmusic = None
            self._token_data = None
            return False

    async def _restore_from_file(self, path: str | None = None) -> bool:
        """Legacy: restore from oauth.json file."""
        import os
        from ytmusicapi import YTMusic

        oauth_path = path or settings.youtube_oauth_json
        if not oauth_path or not os.path.isfile(oauth_path):
            return False

        try:
            async with self._lock:
                self._ytmusic = await asyncio.to_thread(YTMusic, oauth_path)
            logger.info("youtube_auth_restored", method="file", path=oauth_path)
            return True
        except Exception:
            logger.exception("youtube_restore_file_error")
            return False

    async def disconnect(self, db: Database) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        async with self._lock:
            self._ytmusic = None
        self._oauth_credentials = None
        self._token_data = None
        self._verification_url = None
        self._user_code = None
        self._url_cache.clear()
        self._featured_cache = {}
        try:
            await db.execute(
                "DELETE FROM streaming_auth WHERE service = ?", ("youtube",)
            )
            await db.commit()
            logger.info("youtube_disconnected")
        except Exception:
            logger.exception("youtube_disconnect_error")

    async def close(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        async with self._lock:
            self._ytmusic = None
        self._oauth_credentials = None
        self._token_data = None
        self._url_cache.clear()
        self._featured_cache = {}

    # --- Mapping helpers ---

    def _map_track_from_search(self, item: dict) -> Track:
        artists = item.get("artists", [])
        album = item.get("album", {}) or {}
        cover = _best_thumbnail(item.get("thumbnails", []))
        return Track(
            title=item.get("title", "Unknown"),
            artist_name=_first_artist_name(artists),
            album_title=album.get("name") if isinstance(album, dict) else None,
            duration_ms=_parse_duration(item.get("duration", "0:00")),
            format=AudioFormat.OPUS,
            sample_rate=48000,
            bit_depth=16,
            channels=2,
            cover_path=cover,
            source=Source.YOUTUBE,
            source_id=item.get("videoId", ""),
        )

    def _map_track_from_song(self, song: dict) -> Track:
        details = song.get("videoDetails", {})
        cover = _best_thumbnail(
            details.get("thumbnail", {}).get("thumbnails", [])
        )
        return Track(
            title=details.get("title", "Unknown"),
            artist_name=details.get("author", "Unknown"),
            duration_ms=int(details.get("lengthSeconds", 0)) * 1000,
            format=AudioFormat.OPUS,
            sample_rate=48000,
            bit_depth=16,
            channels=2,
            cover_path=cover,
            source=Source.YOUTUBE,
            source_id=details.get("videoId", ""),
        )

    def _map_album_from_search(self, item: dict) -> Album:
        artists = item.get("artists", [])
        return Album(
            title=item.get("title", "Unknown"),
            artist_name=_first_artist_name(artists),
            year=_safe_int(item.get("year")),
            cover_path=_best_thumbnail(item.get("thumbnails", [])),
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
            cover_path=_best_thumbnail(album.get("thumbnails", [])),
            source=Source.YOUTUBE,
            source_id=album_id,
        )

    def _map_artist_from_search(self, item: dict) -> Artist:
        return Artist(
            name=item.get("artist", item.get("title", "Unknown")),
            image_path=_best_thumbnail(item.get("thumbnails", [])),
        )


def _best_thumbnail(thumbnails: list) -> str | None:
    """Return highest-resolution thumbnail URL."""
    if not thumbnails:
        return None
    return thumbnails[-1].get("url")


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

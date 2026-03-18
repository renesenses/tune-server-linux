from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Optional

import aiohttp
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

# YouTube stream URLs expire after ~6 hours — cache for 5h to be safe
_STREAM_URL_TTL = 18_000  # seconds
_YTDLP_TIMEOUT = 30  # seconds

# Google OAuth endpoints
OAUTH_CODE_URL = "https://oauth2.googleapis.com/device/code"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
OAUTH_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
OAUTH_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# YouTube Data API v3
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

DEVICE_POLL_TIMEOUT = 1800  # 30 min

_ISO_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _parse_iso_duration(duration: str) -> int:
    """Parse ISO 8601 duration (e.g. PT4M33S) to milliseconds."""
    m = _ISO_DURATION_RE.match(duration or "")
    if not m:
        return 0
    h, mn, s = (int(x) if x else 0 for x in m.groups())
    return (h * 3600 + mn * 60 + s) * 1000


def _best_thumbnail(thumbnails: dict) -> str | None:
    """Pick the best available thumbnail URL from a snippet.thumbnails dict."""
    for key in ("maxres", "standard", "high", "medium", "default"):
        item = thumbnails.get(key)
        if item and item.get("url"):
            return item["url"]
    return None


class YouTubeService(StreamingService):
    """YouTube streaming service — YouTube Data API v3 + IFrame Player."""

    iframe_only = False  # Hybrid: IFrame (muted) + yt-dlp → DLNA for audio

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expiry: float = 0.0
        self._verification_url: str | None = None
        self._user_code: str | None = None
        self._auth_error: str | None = None
        self._poll_task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._url_cache = StreamUrlCache(ttl_seconds=_STREAM_URL_TTL)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "youtube"

    @property
    def is_authenticated(self) -> bool:
        return self._access_token is not None

    @property
    def verification_url(self) -> str | None:
        return self._verification_url

    @property
    def user_code(self) -> str | None:
        return self._user_code

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _refresh_access_token(self) -> bool:
        """Refresh access token using refresh token."""
        if not self._refresh_token or not settings.youtube_client_id or not settings.youtube_client_secret:
            return False
        try:
            session = await self._ensure_session()
            async with session.post(OAUTH_TOKEN_URL, data={
                "client_id": settings.youtube_client_id,
                "client_secret": settings.youtube_client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            }) as resp:
                if resp.status != 200:
                    logger.warning("youtube_token_refresh_failed", status=resp.status)
                    return False
                data = await resp.json()
                self._access_token = data.get("access_token")
                self._token_expiry = time.time() + data.get("expires_in", 3600) - 60
                logger.info("youtube_token_refreshed")
                return True
        except Exception:
            logger.exception("youtube_token_refresh_error")
            return False

    async def _ensure_token_valid(self) -> bool:
        """Refresh token if expired. Returns True if token is valid."""
        if not self._access_token:
            return False
        if time.time() >= self._token_expiry:
            return await self._refresh_access_token()
        return True

    # ------------------------------------------------------------------
    # Data API v3 helper
    # ------------------------------------------------------------------

    async def _api_get(self, endpoint: str, params: dict) -> dict | None:
        """Make an authenticated GET request to YouTube Data API v3.

        Uses OAuth Bearer token if authenticated, falls back to API key
        (TUNE_YOUTUBE_API_KEY) for unauthenticated quota when available.
        """
        url = f"{YOUTUBE_API_BASE}/{endpoint}"
        headers: dict[str, str] = {}
        req_params = dict(params)

        if self._access_token:
            if not await self._ensure_token_valid():
                return None
            headers["Authorization"] = f"Bearer {self._access_token}"
        elif settings.youtube_api_key:
            req_params["key"] = settings.youtube_api_key
        else:
            return None

        try:
            session = await self._ensure_session()
            async with session.get(url, params=req_params, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "youtube_api_error",
                        endpoint=endpoint,
                        status=resp.status,
                        body=body[:200],
                    )
                    return None
                return await resp.json()
        except Exception:
            logger.exception("youtube_api_request_error", endpoint=endpoint)
            return None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, **kwargs) -> bool:
        db = kwargs.get("db")

        if not settings.youtube_client_id or not settings.youtube_client_secret:
            logger.warning(
                "youtube_auth_no_credentials",
                hint="Set TUNE_YOUTUBE_CLIENT_ID and TUNE_YOUTUBE_CLIENT_SECRET",
            )
            self._auth_error = "missing_credentials"
            return False

        try:
            session = await self._ensure_session()
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
            verification_url = code_data.get("verification_url", "https://google.com/device")
            interval = int(code_data.get("interval", 5))
            expires_in = int(code_data.get("expires_in", DEVICE_POLL_TIMEOUT))

            self._verification_url = verification_url
            self._user_code = user_code

            logger.info("youtube_auth_started",
                        verification_url=verification_url,
                        user_code=user_code)

            # Cancel any previous poll
            if self._poll_task and not self._poll_task.done():
                self._poll_task.cancel()

            self._poll_task = asyncio.create_task(
                self._poll_device_auth(device_code, interval, expires_in, db)
            )

            return False  # waiting for user to authorise on Google

        except Exception:
            logger.exception("youtube_auth_error")
            return False

    async def _poll_device_auth(
        self,
        device_code: str,
        interval: int,
        expires_in: int,
        db: "Database | None",
    ) -> None:
        """Background task: poll Google token endpoint until user authorises."""
        poll_interval = interval
        deadline = time.time() + expires_in

        try:
            session = await self._ensure_session()

            while time.time() < deadline:
                await asyncio.sleep(poll_interval)

                try:
                    async with session.post(OAUTH_TOKEN_URL, data={
                        "client_id": settings.youtube_client_id,
                        "client_secret": settings.youtube_client_secret,
                        "device_code": device_code,
                        "grant_type": OAUTH_GRANT_TYPE,
                    }) as resp:
                        data = await resp.json()
                except Exception:
                    logger.exception("youtube_poll_request_error")
                    continue

                if "access_token" in data:
                    self._access_token = data["access_token"]
                    self._refresh_token = data.get("refresh_token")
                    self._token_expiry = time.time() + data.get("expires_in", 3600) - 60
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
                    logger.warning("youtube_device_auth_failed", error=error)
                    self._verification_url = None
                    self._user_code = None
                    return

            logger.warning("youtube_device_auth_timeout")
            self._verification_url = None
            self._user_code = None

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("youtube_poll_error")
            self._verification_url = None
            self._user_code = None

    # ------------------------------------------------------------------
    # Auth persistence
    # ------------------------------------------------------------------

    async def save_auth(self, db: "Database") -> None:
        if not self._access_token:
            return
        try:
            token_data = json.dumps({
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "token_expiry": self._token_expiry,
            })
            await db.execute(
                "INSERT OR REPLACE INTO streaming_auth (service, token_data, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                ("youtube", token_data),
            )
            await db.commit()
            logger.info("youtube_auth_saved")
        except Exception:
            logger.exception("youtube_save_auth_error")

    async def restore_auth(self, db: "Database") -> bool:
        try:
            row = await db.fetchone(
                "SELECT token_data FROM streaming_auth WHERE service = ?", ("youtube",)
            )
            if not row:
                return False

            data = json.loads(row["token_data"])
            self._access_token = data.get("access_token")
            self._refresh_token = data.get("refresh_token")
            self._token_expiry = float(data.get("token_expiry", 0))

            if not self._access_token:
                return False

            # Refresh immediately if token is expired
            if time.time() >= self._token_expiry:
                refreshed = await self._refresh_access_token()
                if not refreshed:
                    self._access_token = None
                    self._refresh_token = None
                    logger.warning("youtube_auth_restore_refresh_failed")
                    return False

            logger.info("youtube_auth_restored")
            return True

        except Exception:
            logger.exception("youtube_restore_auth_error")
            self._access_token = None
            self._refresh_token = None
            return False

    async def disconnect(self, db: "Database") -> None:
        # Cancel pending auth poll
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

        # Revoke token with Google
        if self._access_token:
            try:
                session = await self._ensure_session()
                async with session.post(OAUTH_REVOKE_URL,
                                        params={"token": self._access_token}):
                    pass
            except Exception:
                pass

        self._access_token = None
        self._refresh_token = None
        self._token_expiry = 0.0
        self._verification_url = None
        self._user_code = None

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
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Streaming — hybrid: yt-dlp audio URL for DLNA, IFrame for visuals
    # ------------------------------------------------------------------

    async def get_stream_url(self, track_id: str) -> Optional[str]:
        """Extract YouTube audio URL via yt-dlp for DLNA backend playback.

        The IFrame Player (frontend) handles the legal/official player requirement
        and is muted. This URL powers the actual audio routed through the zone.
        URLs are cached for ~5h (YouTube CDN URLs expire after ~6h).
        """
        cached = self._url_cache.get(track_id)
        if cached:
            return cached

        try:
            url = await asyncio.wait_for(
                asyncio.to_thread(self._extract_audio_url, track_id),
                timeout=_YTDLP_TIMEOUT,
            )
            if url:
                self._url_cache.set(track_id, url)
                logger.info("youtube_stream_url_resolved", track_id=track_id)
            return url
        except asyncio.TimeoutError:
            logger.warning("youtube_stream_url_timeout", track_id=track_id)
            return None
        except Exception:
            logger.exception("youtube_stream_url_error", track_id=track_id)
            return None

    @staticmethod
    def _extract_audio_url(track_id: str) -> str | None:
        """Run yt-dlp synchronously in a thread to get the best audio URL."""
        from yt_dlp import YoutubeDL

        ydl_opts = {
            # Prefer a non-segmented HTTPS stream (e.g. m4a/aac progressive).
            # "bestaudio" alone often picks a DASH format whose URL only covers
            # the first segment (~50 s). Filtering by protocol=https forces
            # yt-dlp to select a single-file progressive download instead.
            "format": "bestaudio[protocol=https]/bestaudio[protocol=http]/bestaudio",
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
        }
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={track_id}",
                download=False,
            )
            return info.get("url") if info else None

    # ------------------------------------------------------------------
    # Search — Data API v3 (task 2.2)
    # ------------------------------------------------------------------

    async def search(self, query: str, limit: int = 50) -> SearchResult:
        data = await self._api_get("search", {
            "part": "snippet",
            "q": query,
            "maxResults": min(limit, 50),
            "type": "video,playlist,channel",
        })
        if not data:
            return SearchResult()

        video_ids: list[str] = []
        albums: list[Album] = []
        artists: list[Artist] = []

        for item in data.get("items", []):
            kind = item.get("id", {}).get("kind", "")
            snippet = item.get("snippet", {})
            cover = _best_thumbnail(snippet.get("thumbnails", {}))
            title = snippet.get("title", "Unknown")

            if kind == "youtube#video":
                video_ids.append(item["id"]["videoId"])
            elif kind == "youtube#playlist":
                albums.append(Album(
                    title=title,
                    artist_name=snippet.get("channelTitle"),
                    cover_path=cover,
                    source=Source.YOUTUBE,
                    source_id=item["id"]["playlistId"],
                ))
            elif kind == "youtube#channel":
                artists.append(Artist(
                    name=title,
                    image_path=cover,
                    musicbrainz_id=item["id"]["channelId"],
                ))

        # Fetch full video details (duration, etc.) in a single batch call
        tracks = await self._fetch_videos_batch(video_ids)

        return SearchResult(tracks=tracks, albums=albums, artists=artists)

    # ------------------------------------------------------------------
    # Track detail — videos.list (task 2.3)
    # ------------------------------------------------------------------

    async def get_track(self, track_id: str) -> Optional[Track]:
        data = await self._api_get("videos", {
            "part": "snippet,contentDetails",
            "id": track_id,
        })
        if not data:
            return None
        items = data.get("items", [])
        if not items:
            return None
        return self._map_video(items[0])

    def _map_video(self, item: dict) -> Track:
        snippet = item.get("snippet", {})
        content = item.get("contentDetails", {})
        return Track(
            title=snippet.get("title", "Unknown"),
            artist_name=snippet.get("channelTitle"),
            duration_ms=_parse_iso_duration(content.get("duration", "")),
            format=AudioFormat.OPUS,
            sample_rate=48000,
            bit_depth=16,
            channels=2,
            cover_path=_best_thumbnail(snippet.get("thumbnails", {})),
            source=Source.YOUTUBE,
            source_id=item.get("id", ""),
        )

    async def _fetch_videos_batch(self, video_ids: list[str]) -> list[Track]:
        """Fetch full video details for up to N IDs, batched at 50."""
        if not video_ids:
            return []
        tracks: list[Track] = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i + 50]
            data = await self._api_get("videos", {
                "part": "snippet,contentDetails",
                "id": ",".join(batch),
            })
            if data:
                for item in data.get("items", []):
                    tracks.append(self._map_video(item))
        return tracks

    # ------------------------------------------------------------------
    # Album = Playlist — playlists.list + playlistItems (task 2.4)
    # ------------------------------------------------------------------

    async def get_album(self, album_id: str) -> Optional[Album]:
        data = await self._api_get("playlists", {
            "part": "snippet,contentDetails",
            "id": album_id,
        })
        if not data:
            return None
        items = data.get("items", [])
        if not items:
            return None
        return self._map_playlist_as_album(items[0])

    def _map_playlist_as_album(self, item: dict) -> Album:
        snippet = item.get("snippet", {})
        content = item.get("contentDetails", {})
        return Album(
            title=snippet.get("title", "Unknown"),
            artist_name=snippet.get("channelTitle"),
            track_count=content.get("itemCount", 0),
            cover_path=_best_thumbnail(snippet.get("thumbnails", {})),
            source=Source.YOUTUBE,
            source_id=item.get("id", ""),
        )

    async def get_album_tracks(self, album_id: str) -> list[Track]:
        video_ids: list[str] = []
        page_token: str | None = None

        while True:
            params: dict = {
                "part": "snippet",
                "playlistId": album_id,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token

            data = await self._api_get("playlistItems", params)
            if not data:
                break

            for item in data.get("items", []):
                vid = item.get("snippet", {}).get("resourceId", {}).get("videoId")
                if vid:
                    video_ids.append(vid)

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return await self._fetch_videos_batch(video_ids)

    # ------------------------------------------------------------------
    # Artist = Channel — channels.list (task 2.5)
    # ------------------------------------------------------------------

    async def get_artist(self, artist_id: str) -> Optional[Artist]:
        data = await self._api_get("channels", {
            "part": "snippet",
            "id": artist_id,
        })
        if not data:
            return None
        items = data.get("items", [])
        if not items:
            return None
        snippet = items[0].get("snippet", {})
        return Artist(
            name=snippet.get("title", "Unknown"),
            image_path=_best_thumbnail(snippet.get("thumbnails", {})),
        )

    async def get_artist_albums(self, artist_id: str) -> list[Album]:
        data = await self._api_get("playlists", {
            "part": "snippet,contentDetails",
            "channelId": artist_id,
            "maxResults": 50,
        })
        if not data:
            return []
        return [self._map_playlist_as_album(item) for item in data.get("items", [])]

    async def get_artist_tracks(self, artist_id: str) -> list[Track]:
        data = await self._api_get("search", {
            "part": "snippet",
            "channelId": artist_id,
            "type": "video",
            "order": "date",
            "maxResults": 50,
        })
        if not data:
            return []
        video_ids = [
            item["id"]["videoId"]
            for item in data.get("items", [])
            if item.get("id", {}).get("videoId")
        ]
        return await self._fetch_videos_batch(video_ids)

    # ------------------------------------------------------------------
    # User playlists — requires OAuth (task 2.4)
    # ------------------------------------------------------------------

    async def get_user_playlists(self) -> list[StreamingPlaylist]:
        if not self._access_token:
            return []

        playlists: list[StreamingPlaylist] = []
        page_token: str | None = None

        while True:
            params: dict = {
                "part": "snippet,contentDetails",
                "mine": "true",
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token

            data = await self._api_get("playlists", params)
            if not data:
                break

            for item in data.get("items", []):
                snippet = item.get("snippet", {})
                content = item.get("contentDetails", {})
                playlists.append(StreamingPlaylist(
                    source_id=item.get("id", ""),
                    name=snippet.get("title", "Unknown"),
                    description=snippet.get("description") or None,
                    track_count=content.get("itemCount", 0),
                    cover_path=_best_thumbnail(snippet.get("thumbnails", {})),
                    source=Source.YOUTUBE,
                ))

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return playlists

    async def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        return await self.get_album_tracks(playlist_id)

    # ------------------------------------------------------------------
    # Featured — not applicable for YouTube
    # ------------------------------------------------------------------

    async def get_featured_sections(self) -> list[FeaturedSection]:
        return []

    async def get_featured(self, section: str, limit: int = 20) -> list[Album]:
        return []

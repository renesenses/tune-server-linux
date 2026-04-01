from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Optional

import aiohttp
import structlog

from tune_server.config import settings
from tune_server.models import Album, Artist, AudioFormat, SearchResult, Source, Track
from tune_server.streaming.base import StreamingService
from tune_server.streaming.cache import StreamUrlCache

if TYPE_CHECKING:
    from tune_server.db.engine import Database

logger = structlog.get_logger()

AMAZON_AUTH_URL = "https://api.amazon.com/auth/o2/"
AMAZON_TUNE_API = "https://music.amazon.com/api/"
AMAZON_CLIENT_ID = "amzn1.application-oa2-client.music"
DEVICE_POLL_INTERVAL = 5  # seconds
DEVICE_POLL_TIMEOUT = 300  # 5 minutes

# Amazon CDN URLs last ~30min; cache for 10min to provide safety margin
STREAM_URL_TTL = 600

QUALITY_FORMAT_MAP = {
    "SD": AudioFormat.AAC,
    "HD": AudioFormat.FLAC,
    "ULTRA_HD": AudioFormat.FLAC,
}


class AmazonMusicService(StreamingService):
    """Amazon Music streaming service integration.

    NOTE: Uses undocumented API — all calls wrapped in try/except
    with graceful degradation.
    """

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._device_id: str = str(uuid.uuid4())
        self._region: str = settings.amazon_music_region
        self._quality: str = settings.amazon_music_quality
        self._session: aiohttp.ClientSession | None = None
        self._url_cache = StreamUrlCache(ttl_seconds=STREAM_URL_TTL)
        self._refresh_lock = asyncio.Lock()  # Prevent concurrent token refreshes

    @property
    def name(self) -> str:
        return "amazon"

    @property
    def is_authenticated(self) -> bool:
        return self._access_token is not None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _api_request(
        self, method: str, endpoint: str, params: dict | None = None, json_body: dict | None = None
    ) -> dict:
        session = await self._ensure_session()
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "X-Amzn-Device-Id": self._device_id,
            "X-Amzn-Music-Domain": f"music.amazon.{self._region_tld}",
            "Content-Type": "application/json",
        }

        url = f"{AMAZON_TUNE_API}{endpoint}"
        try:
            async with session.request(method, url, params=params, json=json_body, headers=headers) as resp:
                if resp.status == 401:
                    # Token expired — attempt refresh
                    refreshed = await self._refresh_access_token()
                    if refreshed:
                        headers["Authorization"] = f"Bearer {self._access_token}"
                        async with session.request(method, url, params=params, json=json_body, headers=headers) as retry_resp:
                            retry_resp.raise_for_status()
                            return await retry_resp.json()
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=401, message="Amazon auth expired"
                    )
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientResponseError:
            raise
        except Exception:
            logger.exception("amazon_api_request_error", endpoint=endpoint)
            return {}

    @property
    def _region_tld(self) -> str:
        tld_map = {
            "us": "com", "uk": "co.uk", "de": "de", "fr": "fr",
            "it": "it", "es": "es", "jp": "co.jp", "ca": "ca",
            "au": "com.au", "br": "com.br", "mx": "com.mx", "in": "in",
        }
        return tld_map.get(self._region, "com")

    async def authenticate(self, **kwargs) -> bool:
        try:
            session = await self._ensure_session()

            # Step 1: Request device authorization
            auth_data = {
                "response_type": "device_code",
                "client_id": AMAZON_CLIENT_ID,
                "scope": "music::playback music::library",
            }

            async with session.post(f"{AMAZON_AUTH_URL}create/codepair", data=auth_data) as resp:
                if resp.status != 200:
                    logger.warning("amazon_device_auth_request_failed", status=resp.status)
                    return False
                code_data = await resp.json()

            device_code = code_data.get("device_code", "")
            user_code = code_data.get("user_code", "")
            verification_uri = code_data.get("verification_uri", "")

            logger.info(
                "amazon_auth_started",
                user_code=user_code,
                verification_uri=verification_uri,
            )

            # Step 2: Poll for token
            elapsed = 0
            while elapsed < DEVICE_POLL_TIMEOUT:
                await asyncio.sleep(DEVICE_POLL_INTERVAL)
                elapsed += DEVICE_POLL_INTERVAL

                token_data = {
                    "grant_type": "device_code",
                    "device_code": device_code,
                    "client_id": AMAZON_CLIENT_ID,
                }

                async with session.post(f"{AMAZON_AUTH_URL}token", data=token_data) as token_resp:
                    if token_resp.status == 200:
                        tokens = await token_resp.json()
                        self._access_token = tokens.get("access_token")
                        self._refresh_token = tokens.get("refresh_token")
                        logger.info("amazon_authenticated")
                        return True
                    elif token_resp.status == 400:
                        body = await token_resp.json()
                        error = body.get("error", "")
                        if error == "authorization_pending":
                            continue
                        elif error == "slow_down":
                            await asyncio.sleep(DEVICE_POLL_INTERVAL)
                            elapsed += DEVICE_POLL_INTERVAL
                            continue
                        else:
                            logger.warning("amazon_auth_error_response", error=error)
                            return False

            logger.warning("amazon_auth_timeout")
            return False

        except Exception:
            logger.exception("amazon_auth_error")
            return False

    async def _refresh_access_token(self) -> bool:
        if not self._refresh_token:
            return False
        async with self._refresh_lock:
            # Re-check after acquiring lock — another coroutine may have refreshed already
            if not self._refresh_token:
                return False
            try:
                old_token = self._access_token
                session = await self._ensure_session()
                data = {
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": AMAZON_CLIENT_ID,
                }
                async with session.post(f"{AMAZON_AUTH_URL}token", data=data) as resp:
                    if resp.status == 200:
                        tokens = await resp.json()
                        self._access_token = tokens.get("access_token")
                        new_refresh = tokens.get("refresh_token")
                        if new_refresh:
                            self._refresh_token = new_refresh
                        logger.info("amazon_token_refreshed")
                        return True
                    logger.warning("amazon_token_refresh_failed", status=resp.status)
                    return False
            except Exception:
                logger.exception("amazon_token_refresh_error")
                return False

    async def search(self, query: str, limit: int = 50) -> SearchResult:
        if not self._access_token:
            return SearchResult()
        try:
            data = await self._api_request("GET", "search", params={
                "query": query,
                "limit": limit,
            })

            tracks = []
            albums = []
            artists = []

            for hit in data.get("results", []):
                hit_type = hit.get("type", "")
                if hit_type == "track" and len(tracks) < limit:
                    tracks.append(self._map_track(hit))
                elif hit_type == "album" and len(albums) < limit:
                    albums.append(self._map_album(hit))
                elif hit_type == "artist" and len(artists) < limit:
                    artists.append(self._map_artist(hit))

            return SearchResult(tracks=tracks, albums=albums, artists=artists)

        except Exception:
            logger.exception("amazon_search_error")
            return SearchResult()

    async def get_track(self, track_id: str) -> Optional[Track]:
        if not self._access_token:
            return None
        try:
            data = await self._api_request("GET", f"tracks/{track_id}")
            if not data:
                return None
            return self._map_track(data)
        except Exception:
            logger.exception("amazon_get_track_error", track_id=track_id)
            return None

    async def get_album(self, album_id: str) -> Optional[Album]:
        if not self._access_token:
            return None
        try:
            data = await self._api_request("GET", f"albums/{album_id}")
            if not data:
                return None
            return self._map_album(data)
        except Exception:
            logger.exception("amazon_get_album_error", album_id=album_id)
            return None

    async def get_album_tracks(self, album_id: str) -> list[Track]:
        if not self._access_token:
            return []
        try:
            data = await self._api_request("GET", f"albums/{album_id}/tracks")
            return [self._map_track(t) for t in data.get("tracks", [])]
        except Exception:
            logger.exception("amazon_album_tracks_error", album_id=album_id)
            return []

    async def get_artist(self, artist_id: str) -> Optional[Artist]:
        if not self._access_token:
            return None
        try:
            data = await self._api_request("GET", f"artists/{artist_id}")
            if not data:
                return None
            return self._map_artist(data)
        except Exception:
            logger.exception("amazon_get_artist_error", artist_id=artist_id)
            return None

    async def get_artist_albums(self, artist_id: str) -> list[Album]:
        if not self._access_token:
            return []
        try:
            data = await self._api_request("GET", f"artists/{artist_id}/albums")
            return [self._map_album(a) for a in data.get("albums", [])]
        except Exception:
            logger.exception("amazon_artist_albums_error", artist_id=artist_id)
            return []

    async def get_artist_tracks(self, artist_id: str) -> list[Track]:
        if not self._access_token:
            return []
        try:
            data = await self._api_request("GET", f"artists/{artist_id}/tracks")
            return [self._map_track(t) for t in data.get("tracks", [])]
        except Exception:
            logger.exception("amazon_artist_tracks_error", artist_id=artist_id)
            return []

    async def get_stream_url(self, track_id: str) -> Optional[str]:
        if not self._access_token:
            return None
        cached = self._url_cache.get(track_id)
        if cached:
            return cached

        try:
            data = await self._api_request("POST", "stream", json_body={
                "trackId": track_id,
                "quality": self._quality,
                "deviceId": self._device_id,
            })

            url = data.get("url") or data.get("streamUrl")
            if url:
                self._url_cache.set(track_id, url)
            return url

        except Exception:
            logger.exception("amazon_stream_url_error", track_id=track_id)
            return None

    async def save_auth(self, db: Database) -> None:
        if not self._access_token:
            return
        try:
            token_data = json.dumps({
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "device_id": self._device_id,
            })
            await db.execute(
                "INSERT OR REPLACE INTO streaming_auth (service, token_data, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                ("amazon", token_data),
            )
            await db.commit()
            logger.info("amazon_auth_saved")
        except Exception:
            logger.exception("amazon_save_auth_error")

    async def restore_auth(self, db: Database) -> bool:
        try:
            row = await db.fetchone(
                "SELECT token_data FROM streaming_auth WHERE service = ?", ("amazon",)
            )
            if not row:
                return False

            data = json.loads(row["token_data"])
            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            device_id = data.get("device_id")

            if not access_token:
                return False

            self._access_token = access_token
            self._refresh_token = refresh_token
            if device_id:
                self._device_id = device_id

            # Try to refresh token to ensure it's still valid
            if refresh_token:
                refreshed = await self._refresh_access_token()
                if refreshed:
                    logger.info("amazon_auth_restored")
                    return True
                # Refresh failed — token is stale, clear it
                self._access_token = None
                self._refresh_token = None
                logger.warning("amazon_auth_restore_refresh_failed")
                return False

            # No refresh token — can't validate, reject
            self._access_token = None
            logger.warning("amazon_auth_restore_no_refresh_token")
            return False

        except Exception:
            logger.exception("amazon_restore_auth_error")
            return False

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._url_cache.clear()

    # --- Mapping helpers ---

    def _map_track(self, t: dict) -> Track:
        fmt = QUALITY_FORMAT_MAP.get(self._quality, AudioFormat.AAC)
        artist_name = t.get("artist", {}).get("name", "Unknown") if isinstance(t.get("artist"), dict) else t.get("artistName", "Unknown")
        album_title = t.get("album", {}).get("title") if isinstance(t.get("album"), dict) else t.get("albumTitle")

        return Track(
            title=t.get("title", "Unknown"),
            artist_name=artist_name,
            album_title=album_title,
            duration_ms=int(t.get("duration", 0)) * 1000,
            format=fmt,
            sample_rate=44100 if self._quality in ("SD", "HD") else 96000,
            bit_depth=16 if self._quality in ("SD", "HD") else 24,
            channels=2,
            source=Source.AMAZON,
            source_id=str(t.get("id", t.get("trackId", ""))),
        )

    def _map_album(self, a: dict) -> Album:
        artist_name = a.get("artist", {}).get("name", "Unknown") if isinstance(a.get("artist"), dict) else a.get("artistName", "Unknown")
        return Album(
            title=a.get("title", "Unknown"),
            artist_name=artist_name,
            year=a.get("year"),
            track_count=a.get("trackCount", 0),
            source=Source.AMAZON,
            source_id=str(a.get("id", a.get("albumId", ""))),
        )

    def _map_artist(self, ar: dict) -> Artist:
        return Artist(
            name=ar.get("name", ar.get("artistName", "Unknown")),
        )

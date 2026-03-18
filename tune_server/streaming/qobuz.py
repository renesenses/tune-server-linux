from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Optional

import aiohttp
import structlog

from tune_server.config import settings
from tune_server.models import Album, Artist, AudioFormat, FeaturedSection, SearchResult, Source, StreamingPlaylist, Track
from tune_server.streaming.base import StreamingService
from tune_server.streaming.cache import StreamUrlCache

if TYPE_CHECKING:
    from tune_server.db.engine import Database

logger = structlog.get_logger()

QOBUZ_API_BASE = "https://www.qobuz.com/api.json/0.2"


class QobuzService(StreamingService):
    """Qobuz streaming service integration using aiohttp."""

    def __init__(self) -> None:
        self._app_id = settings.qobuz_app_id or ""
        self._app_secret = settings.qobuz_app_secret or ""
        self._user_auth_token: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._url_cache = StreamUrlCache(ttl_seconds=240)

    @property
    def name(self) -> str:
        return "qobuz"

    @property
    def is_authenticated(self) -> bool:
        return self._user_auth_token is not None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _api_get(self, endpoint: str, params: dict = None) -> dict:
        session = await self._ensure_session()
        headers = {"X-App-Id": self._app_id}
        if self._user_auth_token:
            headers["X-User-Auth-Token"] = self._user_auth_token

        url = f"{QOBUZ_API_BASE}/{endpoint}"
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 401:
                logger.warning("qobuz_token_expired")
                self._user_auth_token = None
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history,
                    status=401, message="Qobuz auth expired",
                )
            resp.raise_for_status()
            return await resp.json()

    async def authenticate(self, **kwargs) -> bool:
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")

        if not self._app_id or not self._app_secret:
            logger.warning("qobuz_credentials_missing")
            return False

        try:
            session = await self._ensure_session()
            url = f"{QOBUZ_API_BASE}/user/login"
            data = {
                "email": username,
                "password": password,
                "app_id": self._app_id,
            }
            async with session.post(url, data=data) as resp:
                if resp.status != 200:
                    logger.warning("qobuz_auth_failed", status=resp.status)
                    return False
                data = await resp.json()
                self._user_auth_token = data.get("user_auth_token")
                logger.info("qobuz_authenticated")
                return True

        except Exception:
            logger.exception("qobuz_auth_error")
            return False

    async def search(self, query: str, limit: int = 50) -> SearchResult:
        try:
            data = await self._api_get("catalog/search", {
                "query": query,
                "limit": limit,
            })

            tracks = []
            for t in (data.get("tracks", {}).get("items", []))[:limit]:
                tracks.append(self._map_track(t))

            albums = []
            for a in (data.get("albums", {}).get("items", []))[:limit]:
                albums.append(self._map_album(a))

            artists = []
            for ar in (data.get("artists", {}).get("items", []))[:limit]:
                artists.append(self._map_artist(ar))

            return SearchResult(tracks=tracks, albums=albums, artists=artists)

        except Exception:
            logger.exception("qobuz_search_error")
            return SearchResult()

    async def get_track(self, track_id: str) -> Optional[Track]:
        try:
            data = await self._api_get("track/get", {"track_id": track_id})
            return self._map_track(data)
        except Exception:
            logger.exception("qobuz_get_track_error")
            return None

    async def get_album(self, album_id: str) -> Optional[Album]:
        try:
            data = await self._api_get("album/get", {"album_id": album_id})
            return self._map_album(data)
        except Exception:
            return None

    async def get_album_tracks(self, album_id: str) -> list[Track]:
        try:
            data = await self._api_get("album/get", {"album_id": album_id})
            items = data.get("tracks", {}).get("items", [])
            return [self._map_track(t) for t in items]
        except Exception:
            return []

    async def get_artist(self, artist_id: str) -> Optional[Artist]:
        try:
            data = await self._api_get("artist/get", {"artist_id": artist_id})
            return self._map_artist(data)
        except Exception:
            return None

    async def get_artist_albums(self, artist_id: str) -> list[Album]:
        try:
            data = await self._api_get("artist/get", {"artist_id": artist_id, "extra": "albums"})
            items = data.get("albums", {}).get("items", [])
            return [self._map_album(a) for a in items]
        except Exception:
            logger.exception("qobuz_artist_albums_error", artist_id=artist_id)
            return []

    async def get_artist_tracks(self, artist_id: str) -> list[Track]:
        try:
            # Qobuz has no direct artist-tracks endpoint; gather from top albums
            albums = await self.get_artist_albums(artist_id)
            tracks: list[Track] = []
            for album in albums[:5]:
                album_tracks = await self.get_album_tracks(album.source_id)
                tracks.extend(album_tracks)
            return tracks
        except Exception:
            logger.exception("qobuz_artist_tracks_error", artist_id=artist_id)
            return []

    async def get_stream_url(self, track_id: str) -> Optional[str]:
        cached = self._url_cache.get(track_id)
        if cached:
            return cached

        try:
            import time
            unix = time.time()
            sig_data = f"trackgetFileUrlformat_id27intentstreamtrack_id{track_id}{unix}{self._app_secret}"
            sig = hashlib.md5(sig_data.encode("utf-8")).hexdigest()

            data = await self._api_get("track/getFileUrl", {
                "track_id": track_id,
                "format_id": 27,
                "intent": "stream",
                "request_ts": unix,
                "request_sig": sig,
            })

            url = data.get("url")
            if url:
                self._url_cache.set(track_id, url)
            return url

        except Exception:
            logger.exception("qobuz_stream_url_error")
            return None

    async def get_featured_sections(self) -> list[FeaturedSection]:
        return [
            FeaturedSection(id="new-releases", name="Nouvelles Sorties"),
            FeaturedSection(id="best-sellers", name="Meilleures Ventes"),
            FeaturedSection(id="press-awards", name="Récompenses Presse"),
            FeaturedSection(id="editor-picks", name="Sélection de la Rédaction"),
        ]

    async def get_featured(self, section: str, limit: int = 20) -> list[Album]:
        try:
            data = await self._api_get("album/getFeatured", {"type": section, "limit": limit})
            albums = data.get("albums", {}).get("items", [])
            return [self._map_album(a) for a in albums]
        except Exception:
            logger.exception("qobuz_get_featured_error", section=section)
            return []

    async def get_user_playlists(self) -> list[StreamingPlaylist]:
        try:
            data = await self._api_get("playlist/getUserPlaylists")
            items = data.get("playlists", {}).get("items", [])
            return [self._map_playlist(p) for p in items]
        except Exception:
            logger.exception("qobuz_user_playlists_error")
            return []

    async def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        try:
            all_items: list[dict] = []
            offset = 0
            limit = 500
            while True:
                data = await self._api_get("playlist/get", {
                    "playlist_id": playlist_id,
                    "extra": "tracks",
                    "limit": limit,
                    "offset": offset,
                })
                tracks_data = data.get("tracks", {})
                items = tracks_data.get("items", [])
                all_items.extend(items)
                total = tracks_data.get("total", 0)
                if len(all_items) >= total or len(items) == 0:
                    break
                offset += len(items)
            return [self._map_track(t) for t in all_items]
        except Exception:
            logger.exception("qobuz_playlist_tracks_error", playlist_id=playlist_id)
            return []

    def _map_playlist(self, p: dict) -> StreamingPlaylist:
        image = p.get("images300", [])
        cover_path = image[0] if image else None
        return StreamingPlaylist(
            source_id=str(p.get("id", "")),
            name=p.get("name", "Unknown"),
            description=p.get("description"),
            track_count=p.get("tracks_count", 0),
            duration_ms=int(p.get("duration", 0)) * 1000,
            cover_path=cover_path,
            source=Source.QOBUZ,
        )

    async def save_auth(self, db: Database) -> None:
        if not self._user_auth_token:
            return
        try:
            token_data = json.dumps({"user_auth_token": self._user_auth_token})
            await db.execute(
                "INSERT OR REPLACE INTO streaming_auth (service, token_data, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                ("qobuz", token_data),
            )
            await db.commit()
            logger.info("qobuz_auth_saved")
        except Exception:
            logger.exception("qobuz_save_auth_error")

    async def restore_auth(self, db: Database) -> bool:
        try:
            row = await db.fetchone(
                "SELECT token_data FROM streaming_auth WHERE service = ?", ("qobuz",)
            )
            if not row:
                return False

            data = json.loads(row["token_data"])
            token = data.get("user_auth_token")
            if token:
                self._user_auth_token = token
                logger.info("qobuz_auth_restored")
                return True
            return False
        except Exception:
            logger.exception("qobuz_restore_auth_error")
            return False

    async def disconnect(self, db: Database) -> None:
        self._user_auth_token = None
        try:
            await db.execute(
                "DELETE FROM streaming_auth WHERE service = ?", ("qobuz",)
            )
            await db.commit()
            logger.info("qobuz_disconnected")
        except Exception:
            logger.exception("qobuz_disconnect_error")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _map_track(self, t: dict) -> Track:
        performer = t.get("performer", {})
        album = t.get("album", {})
        image = album.get("image", {}) if album else {}
        cover_path = image.get("large") or image.get("small") or image.get("thumbnail") or None

        return Track(
            title=t.get("title", "Unknown"),
            artist_name=performer.get("name", "Unknown"),
            album_title=album.get("title"),
            duration_ms=int(t.get("duration", 0)) * 1000,
            format=AudioFormat.FLAC,
            sample_rate=t.get("maximum_sampling_rate", 44.1) * 1000,
            bit_depth=t.get("maximum_bit_depth", 16),
            channels=2,
            cover_path=cover_path,
            source=Source.QOBUZ,
            source_id=str(t.get("id", "")),
        )

    def _map_album(self, a: dict) -> Album:
        artist = a.get("artist", {})
        image = a.get("image", {})
        cover_path = image.get("large") or image.get("small") or image.get("thumbnail") or None
        return Album(
            title=a.get("title", "Unknown"),
            artist_name=artist.get("name", "Unknown"),
            year=a.get("released_at", 0) // 10000000000 if a.get("released_at") else None,
            track_count=a.get("tracks_count", 0),
            cover_path=cover_path,
            source=Source.QOBUZ,
            source_id=str(a.get("id", "")),
        )

    def _map_artist(self, ar: dict) -> Artist:
        return Artist(
            name=ar.get("name", "Unknown"),
            source_id=str(ar.get("id", "")),
            image_path=ar.get("image", {}).get("large") if isinstance(ar.get("image"), dict) else None,
        )

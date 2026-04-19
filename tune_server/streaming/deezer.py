from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Optional

import aiohttp
import structlog

from tune_server.config import settings
from tune_server.models import (
    Album, Artist, AudioFormat, FeaturedSection, SearchResult,
    Source, StreamingPlaylist, Track,
)
from tune_server.streaming.base import StreamingService, http_request_with_retry
from tune_server.streaming.cache import StreamUrlCache

if TYPE_CHECKING:
    from tune_server.db.engine import Database

logger = structlog.get_logger()

# Deezer public API (no auth needed for catalog)
DEEZER_API = "https://api.deezer.com"

# Gateway API (requires ARL cookie)
DEEZER_GW = "https://www.deezer.com/ajax/gw-light.php"

# Media URL endpoint for full stream URLs
DEEZER_MEDIA_URL = "https://media.deezer.com/v1"

# Stream URL TTL
STREAM_URL_TTL = 3600


class DeezerService(StreamingService):
    """Deezer streaming service integration.

    Uses the public API for catalog browsing (no auth needed) and the
    gateway API with ARL cookie for full track streaming.
    """

    def __init__(self, *, arl: str | None = None, quality: str = "FLAC") -> None:
        self._arl: str | None = arl
        self._quality: str = quality
        self._session: aiohttp.ClientSession | None = None
        self._url_cache = StreamUrlCache(ttl_seconds=STREAM_URL_TTL)
        self._license_token: str | None = None
        self._user_id: str | None = None
        self._api_token: str | None = None

    @property
    def name(self) -> str:
        return "deezer"

    @property
    def is_authenticated(self) -> bool:
        return self._arl is not None and self._license_token is not None

    # ------------------------------------------------------------------
    # HTTP Client
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=settings.api_timeout, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _api_get(self, endpoint: str, params: dict | None = None) -> dict:
        """Public API GET (no auth needed for catalog data)."""
        session = await self._ensure_session()
        url = f"{DEEZER_API}/{endpoint}"
        try:
            resp = await http_request_with_retry(
                session, "GET", url,
                params=params,
                service_name="deezer",
            )
            async with resp:
                if resp.status != 200:
                    logger.warning("deezer_api_error", endpoint=endpoint, status=resp.status)
                    return {}
                return await resp.json()
        except Exception:
            logger.exception("deezer_api_error", endpoint=endpoint)
            return {}

    async def _gw_api_call(self, method: str, params: dict | None = None) -> dict:
        """Internal gateway API call (requires ARL cookie)."""
        if not self._arl:
            return {}
        session = await self._ensure_session()
        query = {
            "method": method,
            "input": "3",
            "api_version": "1.0",
            "api_token": self._api_token or "",
        }
        cookies = {"arl": self._arl}
        try:
            resp = await http_request_with_retry(
                session, "POST", DEEZER_GW,
                params=query, json=params or {}, cookies=cookies,
                service_name="deezer",
            )
            async with resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                # Capture api_token from first call for subsequent ones
                if not self._api_token and "checkForm" in data.get("results", {}):
                    self._api_token = data["results"]["checkForm"]
                return data.get("results", {})
        except Exception:
            logger.exception("deezer_gw_error", method=method)
            return {}

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, **kwargs) -> bool:
        """Authenticate with Deezer using ARL cookie."""
        arl = kwargs.get("arl") or self._arl
        if not arl:
            logger.warning("deezer_auth_no_arl")
            return False

        self._arl = arl

        try:
            result = await self._gw_api_call("deezer.getUserData")
            if not result:
                logger.warning("deezer_auth_failed")
                self._arl = None
                return False

            user = result.get("USER", {})
            self._user_id = str(user.get("USER_ID", ""))
            self._license_token = user.get("OPTIONS", {}).get("license_token")
            # Capture api_token
            if "checkForm" in result:
                self._api_token = result["checkForm"]

            if not self._user_id or self._user_id == "0":
                logger.warning("deezer_auth_invalid_arl")
                self._arl = None
                self._license_token = None
                return False

            user_name = user.get("BLOG_NAME", self._user_id)
            logger.info("deezer_authenticated", user_id=self._user_id, name=user_name)
            return True

        except Exception:
            logger.exception("deezer_auth_error")
            self._arl = None
            return False

    async def disconnect(self, db: "Database") -> None:
        self._arl = None
        self._license_token = None
        self._user_id = None
        self._api_token = None
        self._url_cache.clear()
        await db.execute("DELETE FROM streaming_auth WHERE service = ?", ("deezer",))
        await db.commit()
        logger.info("deezer_disconnected")

    async def set_quality(self, quality: str) -> None:
        self._quality = quality
        self._url_cache.clear()

    def get_quality(self) -> str:
        return self._quality

    # ------------------------------------------------------------------
    # Auth Persistence
    # ------------------------------------------------------------------

    async def save_auth(self, db: "Database") -> None:
        if not self._arl:
            return
        try:
            token_data = json.dumps({
                "arl": self._arl,
                "quality": self._quality,
            })
            await db.execute(
                "INSERT INTO streaming_auth (service, token_data, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT (service) DO UPDATE SET token_data = excluded.token_data, "
                "updated_at = CURRENT_TIMESTAMP",
                ("deezer", token_data),
            )
            await db.commit()
            logger.info("deezer_auth_saved")
        except Exception:
            logger.exception("deezer_save_auth_error")

    async def restore_auth(self, db: "Database") -> bool:
        try:
            row = await db.fetchone(
                "SELECT token_data FROM streaming_auth WHERE service = ?", ("deezer",)
            )
            if not row:
                return False

            data = json.loads(row["token_data"])
            arl = data.get("arl")
            quality = data.get("quality", "FLAC")

            if not arl:
                return False

            self._quality = quality
            result = await self.authenticate(arl=arl)
            if result:
                logger.info("deezer_auth_restored")
            return result

        except Exception:
            logger.exception("deezer_restore_auth_error")
            return False

    # ------------------------------------------------------------------
    # Search (public API, no auth needed)
    # ------------------------------------------------------------------

    async def search(self, query: str, limit: int = 50) -> SearchResult:
        try:
            tracks = []
            albums = []
            artists = []

            data = await self._api_get("search/track", {"q": query, "limit": limit})
            for item in data.get("data", []):
                tracks.append(self._map_track(item))

            data = await self._api_get("search/album", {"q": query, "limit": limit})
            for item in data.get("data", []):
                albums.append(self._map_album(item))

            data = await self._api_get("search/artist", {"q": query, "limit": limit})
            for item in data.get("data", []):
                artists.append(self._map_artist(item))

            return SearchResult(tracks=tracks, albums=albums, artists=artists)

        except Exception:
            logger.exception("deezer_search_error")
            return SearchResult()

    # ------------------------------------------------------------------
    # Entity Retrieval
    # ------------------------------------------------------------------

    async def get_track(self, track_id: str) -> Optional[Track]:
        try:
            data = await self._api_get(f"track/{track_id}")
            if data.get("error"):
                return None
            return self._map_track(data)
        except Exception:
            logger.exception("deezer_get_track_error", track_id=track_id)
            return None

    async def get_album(self, album_id: str) -> Optional[Album]:
        try:
            data = await self._api_get(f"album/{album_id}")
            if data.get("error"):
                return None
            return self._map_album_detail(data)
        except Exception:
            logger.exception("deezer_get_album_error", album_id=album_id)
            return None

    async def get_album_tracks(self, album_id: str) -> list[Track]:
        try:
            data = await self._api_get(f"album/{album_id}/tracks", {"limit": 200})
            # Get album info for cover art
            album_data = await self._api_get(f"album/{album_id}")
            album_cover = album_data.get("cover_big") or album_data.get("cover_medium")
            album_title = album_data.get("title")

            tracks = []
            for t in data.get("data", []):
                track = self._map_track(t)
                # Fill in album info from parent
                if not track.cover_path and album_cover:
                    track.cover_path = album_cover
                if not track.album_title and album_title:
                    track.album_title = album_title
                tracks.append(track)
            return tracks
        except Exception:
            logger.exception("deezer_album_tracks_error", album_id=album_id)
            return []

    async def get_artist(self, artist_id: str) -> Optional[Artist]:
        try:
            data = await self._api_get(f"artist/{artist_id}")
            if data.get("error"):
                return None
            return Artist(
                name=data.get("name", "Unknown"),
                source_id=str(data.get("id", "")),
                image_path=data.get("picture_big") or data.get("picture_medium"),
            )
        except Exception:
            logger.exception("deezer_get_artist_error", artist_id=artist_id)
            return None

    async def get_artist_albums(self, artist_id: str) -> list[Album]:
        try:
            data = await self._api_get(f"artist/{artist_id}/albums", {"limit": 100})
            return [self._map_album(a) for a in data.get("data", [])]
        except Exception:
            logger.exception("deezer_artist_albums_error", artist_id=artist_id)
            return []

    async def get_artist_tracks(self, artist_id: str) -> list[Track]:
        try:
            data = await self._api_get(f"artist/{artist_id}/top", {"limit": 50})
            return [self._map_track(t) for t in data.get("data", [])]
        except Exception:
            logger.exception("deezer_artist_tracks_error", artist_id=artist_id)
            return []

    # ------------------------------------------------------------------
    # Playlists
    # ------------------------------------------------------------------

    async def get_user_playlists(self) -> list[StreamingPlaylist]:
        if not self.is_authenticated:
            return []
        try:
            data = await self._api_get(f"user/{self._user_id}/playlists", {"limit": 200})
            return [self._map_playlist(p) for p in data.get("data", [])]
        except Exception:
            logger.exception("deezer_playlists_error")
            return []

    async def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        try:
            all_tracks = []
            index = 0
            limit = 200
            while True:
                data = await self._api_get(
                    f"playlist/{playlist_id}/tracks",
                    {"limit": limit, "index": index},
                )
                items = data.get("data", [])
                all_tracks.extend([self._map_track(t) for t in items])
                total = data.get("total", 0)
                if len(all_tracks) >= total or len(items) == 0:
                    break
                index += len(items)
            return all_tracks
        except Exception:
            logger.exception("deezer_playlist_tracks_error", playlist_id=playlist_id)
            return []

    # ------------------------------------------------------------------
    # Featured
    # ------------------------------------------------------------------

    async def get_featured_sections(self) -> list[FeaturedSection]:
        return [
            FeaturedSection(id="chart", name="Top Charts"),
            FeaturedSection(id="new-releases", name="Nouveautes"),
            FeaturedSection(id="selection", name="Selection Deezer"),
        ]

    async def get_featured(self, section: str, limit: int = 20) -> list[Album]:
        try:
            if section == "chart":
                data = await self._api_get("chart/0/albums", {"limit": limit})
                return [self._map_album(a) for a in data.get("data", [])]
            elif section == "new-releases":
                data = await self._api_get("editorial/0/releases", {"limit": limit})
                return [self._map_album(a) for a in data.get("data", [])]
            elif section == "selection":
                data = await self._api_get("editorial/0/selection", {"limit": limit})
                albums = []
                for item in data.get("data", []):
                    if item.get("type") == "album":
                        albums.append(self._map_album(item))
                return albums
            return []
        except Exception:
            logger.exception("deezer_featured_error", section=section)
            return []

    # ------------------------------------------------------------------
    # Stream URL
    # ------------------------------------------------------------------

    async def get_stream_url(self, track_id: str) -> Optional[str]:
        cached = self._url_cache.get(track_id)
        if cached:
            return cached

        # Try full track URL if authenticated
        if self._arl and self._license_token:
            try:
                url = await self._get_full_stream_url(track_id)
                if url:
                    self._url_cache.set(track_id, url)
                    return url
            except Exception:
                logger.exception("deezer_full_stream_error", track_id=track_id)

        # Fallback to 30s preview
        try:
            data = await self._api_get(f"track/{track_id}")
            preview = data.get("preview")
            if preview:
                self._url_cache.set(track_id, preview)
            return preview
        except Exception:
            logger.exception("deezer_stream_url_error", track_id=track_id)
            return None

    async def _get_full_stream_url(self, track_id: str) -> str | None:
        """Get full track stream URL via gateway API (requires ARL)."""
        result = await self._gw_api_call("song.getData", {"SNG_ID": track_id})
        if not result:
            return None

        token = result.get("TRACK_TOKEN")
        if not token:
            return None

        quality_map = {
            "MP3_128": "1",
            "MP3_320": "3",
            "FLAC": "9",
        }
        format_id = quality_map.get(self._quality, "9")

        session = await self._ensure_session()
        try:
            resp = await http_request_with_retry(
                session, "POST", f"{DEEZER_MEDIA_URL}/get_url",
                json={
                    "license_token": self._license_token,
                    "media": [{
                        "type": "FULL",
                        "formats": [{"cipher": "BF_CBC_STRIPE", "format": format_id}],
                    }],
                    "track_tokens": [token],
                },
                service_name="deezer",
            )
            async with resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                media = data.get("data", [{}])[0].get("media", [{}])
                if media:
                    sources = media[0].get("sources", [])
                    if sources:
                        url = sources[0].get("url")
                        logger.info("deezer_stream_url_resolved",
                                    track_id=track_id, quality=self._quality)
                        return url
        except Exception:
            logger.exception("deezer_media_url_error", track_id=track_id)

        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._url_cache.clear()

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    def _map_track(self, t: dict) -> Track:
        artist = t.get("artist", {})
        album = t.get("album", {})

        # Cover art: use album cover
        cover = None
        if isinstance(album, dict):
            cover = album.get("cover_big") or album.get("cover_medium") or album.get("cover")

        fmt = AudioFormat.FLAC if self._quality == "FLAC" else AudioFormat.MP3
        bit_depth = 16
        sample_rate = 44100

        return Track(
            title=t.get("title", "Unknown"),
            artist_name=artist.get("name", "Unknown") if isinstance(artist, dict) else "Unknown",
            album_title=album.get("title") if isinstance(album, dict) else None,
            duration_ms=int(t.get("duration", 0)) * 1000,
            format=fmt,
            sample_rate=sample_rate,
            bit_depth=bit_depth,
            channels=2,
            cover_path=cover,
            source=Source.DEEZER,
            source_id=str(t.get("id", "")),
        )

    def _map_album(self, a: dict) -> Album:
        artist = a.get("artist", {})
        cover = a.get("cover_big") or a.get("cover_medium") or a.get("cover")
        return Album(
            title=a.get("title", "Unknown"),
            artist_name=artist.get("name", "Unknown") if isinstance(artist, dict) else "Unknown",
            track_count=a.get("nb_tracks", 0),
            cover_path=cover,
            source=Source.DEEZER,
            source_id=str(a.get("id", "")),
        )

    def _map_album_detail(self, a: dict) -> Album:
        artist = a.get("artist", {})
        cover = a.get("cover_big") or a.get("cover_medium") or a.get("cover")
        genres = a.get("genres", {}).get("data", [])
        genre = genres[0].get("name") if genres else None
        return Album(
            title=a.get("title", "Unknown"),
            artist_name=artist.get("name", "Unknown") if isinstance(artist, dict) else "Unknown",
            year=int(a.get("release_date", "0")[:4]) if a.get("release_date") else None,
            track_count=a.get("nb_tracks", 0),
            genre=genre,
            cover_path=cover,
            source=Source.DEEZER,
            source_id=str(a.get("id", "")),
        )

    def _map_artist(self, ar: dict) -> Artist:
        return Artist(
            name=ar.get("name", "Unknown"),
            source_id=str(ar.get("id", "")),
            image_path=ar.get("picture_big") or ar.get("picture_medium"),
        )

    def _map_playlist(self, p: dict) -> StreamingPlaylist:
        return StreamingPlaylist(
            source_id=str(p.get("id", "")),
            name=p.get("title", "Unknown"),
            description=p.get("description"),
            track_count=p.get("nb_tracks", 0),
            duration_ms=int(p.get("duration", 0)) * 1000,
            cover_path=p.get("picture_big") or p.get("picture_medium"),
            source=Source.DEEZER,
        )

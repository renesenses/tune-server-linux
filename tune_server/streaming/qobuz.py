from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Optional

import aiohttp
import structlog

from tune_server.config import settings
from tune_server.models import Album, Artist, AudioFormat, FeaturedSection, SearchResult, Source, StreamingGenre, StreamingPlaylist, Track
from tune_server.streaming.base import StreamingService, http_request_with_retry
from tune_server.streaming.cache import StreamUrlCache
from urllib.parse import quote as _url_quote


def _proxy_cover(url: str | None) -> str | None:
    if not url or not url.startswith("http"):
        return url
    return f"/api/v1/library/artwork/proxy?url={_url_quote(url, safe='')}"

if TYPE_CHECKING:
    from tune_server.db.engine import Database

logger = structlog.get_logger()

QOBUZ_API_BASE = "https://www.qobuz.com/api.json/0.2"
QOBUZ_API_PROXY = "https://mozaiklabs.fr/qobuz-api"
QOBUZ_LOGIN_APP_ID = "312369995"


class QobuzService(StreamingService):
    """Qobuz streaming service integration using aiohttp."""

    _REMOTE_CONFIG_URL = "https://mozaiklabs.fr/storage/api/v1/streaming-config.json"

    def __init__(self) -> None:
        self._app_id = settings.qobuz_app_id or ""
        self._app_secret = settings.qobuz_app_secret or ""
        self._user_auth_token: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._url_cache = StreamUrlCache(ttl_seconds=240)
        self._credentials_refreshed = False
        self._use_proxy = False
        self._login_app_id: str | None = None

    @property
    def name(self) -> str:
        return "qobuz"

    @property
    def is_authenticated(self) -> bool:
        return self._user_auth_token is not None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=settings.api_timeout, connect=10)
            connector = None
            proxy_url = settings.qobuz_proxy_url
            if proxy_url:
                # Akamai blacklists most Free / OVH / datacenter ASNs at the
                # CDN edge — direct calls hit a hard 403 even with valid
                # creds. Tunnel through the configured proxy (SOCKS5 from
                # NordVPN/Mullvad, HTTP from a residential gateway, etc.).
                # Other streaming services (Tidal, Deezer, Spotify) are not
                # affected and stay on the default direct connection.
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(proxy_url)
                logger.info("qobuz_proxy_enabled", proxy=proxy_url.split("@")[-1])
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self._session

    @property
    def _api_base(self) -> str:
        return QOBUZ_API_PROXY if self._use_proxy else QOBUZ_API_BASE

    async def _api_get(self, endpoint: str, params: dict = None, _retry: bool = True) -> dict:
        session = await self._ensure_session()
        headers = {"X-App-Id": self._login_app_id or self._app_id}
        if self._user_auth_token:
            headers["X-User-Auth-Token"] = self._user_auth_token

        url = f"{self._api_base}/{endpoint}"
        try:
            resp = await http_request_with_retry(
                session, "GET", url,
                params=params, headers=headers,
                service_name="qobuz",
            )
        except aiohttp.ClientResponseError as exc:
            if exc.status == 401:
                logger.warning("qobuz_token_expired")
                self._user_auth_token = None
            elif exc.status == 403 and not self._use_proxy:
                logger.warning("qobuz_blocked_switching_to_proxy", endpoint=endpoint)
                self._use_proxy = True
                return await self._api_get(endpoint, params, _retry=_retry)
            elif exc.status == 403 and _retry:
                logger.warning("qobuz_forbidden_refreshing_credentials", endpoint=endpoint)
                self._credentials_refreshed = False
                await self._refresh_credentials()
                return await self._api_get(endpoint, params, _retry=False)
            raise
        except Exception:
            if self._use_proxy:
                logger.warning("qobuz_proxy_failed_falling_back_to_direct", endpoint=endpoint)
                self._use_proxy = False
                return await self._api_get(endpoint, params, _retry=_retry)
            raise

        async with resp:
            resp.raise_for_status()
            return await resp.json()

    async def _refresh_credentials(self) -> None:
        """Fetch latest Qobuz app credentials from mozaiklabs.fr."""
        if self._credentials_refreshed:
            return
        try:
            session = await self._ensure_session()
            async with session.get(self._REMOTE_CONFIG_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    qobuz = data.get("qobuz", {})
                    new_id = qobuz.get("app_id")
                    new_secret = qobuz.get("app_secret")
                    if new_id and new_secret:
                        if new_id != self._app_id or new_secret != self._app_secret:
                            logger.info("qobuz_credentials_updated", old_id=self._app_id[:4], new_id=new_id[:4])
                        self._app_id = new_id
                        self._app_secret = new_secret
            self._credentials_refreshed = True
        except Exception:
            logger.debug("qobuz_remote_config_unavailable")

    async def authenticate(self, **kwargs) -> bool:
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")

        # Auto-refresh credentials from mozaiklabs.fr
        await self._refresh_credentials()

        if not self._app_id or not self._app_secret:
            logger.warning("qobuz_credentials_missing")
            return False

        try:
            session = await self._ensure_session()
            url = f"{self._api_base}/user/login"
            login_app_id = QOBUZ_LOGIN_APP_ID if QOBUZ_LOGIN_APP_ID else self._app_id
            data = {
                "email": username,
                "password": password,
                "app_id": login_app_id,
            }
            async with session.post(url, data=data) as resp:
                if resp.status == 403 and not self._use_proxy:
                    logger.warning("qobuz_auth_blocked_switching_to_proxy")
                    self._use_proxy = True
                    return await self.authenticate(**kwargs)
                if resp.status != 200:
                    logger.warning("qobuz_auth_failed", status=resp.status)
                    return False
                data = await resp.json()
                self._user_auth_token = data.get("user_auth_token")
                self._login_app_id = login_app_id
                logger.info("qobuz_authenticated", proxy=self._use_proxy, app_id=login_app_id[:4])
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
        except Exception as e:
            logger.debug("qobuz_get_album_failed", album_id=album_id, error=str(e))
            return None

    async def get_album_tracks(self, album_id: str) -> list[Track]:
        try:
            data = await self._api_get("album/get", {"album_id": album_id})
            items = data.get("tracks", {}).get("items", [])
            # The Qobuz API sometimes omits the album image in each track
            # object when tracks are returned inside an album response.
            # Extract the album-level image and inject it into tracks that
            # lack their own so _map_track can find it.
            album_image = data.get("image", {})
            tracks = []
            for t in items:
                track_album = t.get("album") or {}
                if isinstance(track_album, dict) and not track_album.get("image"):
                    track_album = {**track_album, "image": album_image}
                    t = {**t, "album": track_album}
                tracks.append(self._map_track(t))
            return tracks
        except Exception as e:
            logger.debug("qobuz_get_album_tracks_failed", album_id=album_id, error=str(e))
            return []

    async def get_artist(self, artist_id: str) -> Optional[Artist]:
        try:
            data = await self._api_get("artist/get", {"artist_id": artist_id})
            return self._map_artist(data)
        except Exception as e:
            logger.debug("qobuz_get_artist_failed", artist_id=artist_id, error=str(e))
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

    async def get_genres(self, parent_id: str | None = None) -> list[StreamingGenre]:
        """Fetch genre list from Qobuz. If parent_id is given, returns sub-genres."""
        try:
            params: dict = {}
            if parent_id:
                params["parent_id"] = parent_id
            data = await self._api_get("genre/list", params or None)
            genres_data = data.get("genres", {}).get("items", [])
            result: list[StreamingGenre] = []
            for g in genres_data:
                slug = g.get("slug", "")
                # Qobuz sub-genres have a path like "parent/child"
                has_children = bool(g.get("subgenresCount", 0)) or len(slug.split("/")) == 1
                result.append(StreamingGenre(
                    id=str(g.get("id", "")),
                    name=g.get("name", "Unknown"),
                    has_children=has_children,
                    image_url=g.get("image", {}).get("large") if isinstance(g.get("image"), dict) else None,
                ))
            return result
        except Exception:
            logger.exception("qobuz_get_genres_error", parent_id=parent_id)
            return []

    async def get_genre_albums(self, genre_id: str, limit: int = 50) -> list[Album]:
        """Fetch albums for a given Qobuz genre."""
        try:
            data = await self._api_get("album/getFeatured", {
                "type": "new-releases",
                "genre_id": genre_id,
                "genre_ids": genre_id,
                "limit": limit,
            })
            albums = data.get("albums", {}).get("items", [])
            return [self._map_album(a) for a in albums]
        except Exception:
            logger.exception("qobuz_get_genre_albums_error", genre_id=genre_id)
            return []

    async def get_user_favorites(self, fav_type: str = "tracks", limit: int = 500) -> dict:
        """Fetch user favorites (tracks, albums, or artists) from Qobuz."""
        try:
            all_items: list[dict] = []
            offset = 0
            while True:
                data = await self._api_get("favorite/getUserFavorites", {
                    "type": fav_type,
                    "limit": limit,
                    "offset": offset,
                })
                container = data.get(fav_type, {})
                items = container.get("items", [])
                all_items.extend(items)
                total = container.get("total", 0)
                offset += len(items)
                if offset >= total or not items:
                    break

            if fav_type == "tracks":
                return {"tracks": [self._map_track(t) for t in all_items]}
            elif fav_type == "albums":
                return {"albums": [self._map_album(a) for a in all_items]}
            elif fav_type == "artists":
                return {"artists": [self._map_artist(a) for a in all_items]}
            return {}
        except Exception:
            logger.exception("qobuz_user_favorites_error", type=fav_type)
            return {}

    async def add_favorite(self, fav_type: str, item_id: str) -> bool:
        try:
            id_key = {"tracks": "track_ids", "albums": "album_ids", "artists": "artist_ids"}.get(fav_type)
            if not id_key:
                return False
            await self._api_get("favorite/create", {id_key: item_id})
            logger.info("qobuz_favorite_added", type=fav_type, id=item_id)
            return True
        except Exception:
            logger.exception("qobuz_favorite_add_error", type=fav_type, id=item_id)
            return False

    async def remove_favorite(self, fav_type: str, item_id: str) -> bool:
        try:
            id_key = {"tracks": "track_ids", "albums": "album_ids", "artists": "artist_ids"}.get(fav_type)
            if not id_key:
                return False
            await self._api_get("favorite/delete", {id_key: item_id})
            logger.info("qobuz_favorite_removed", type=fav_type, id=item_id)
            return True
        except Exception:
            logger.exception("qobuz_favorite_remove_error", type=fav_type, id=item_id)
            return False

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
            token_data = json.dumps({"user_auth_token": self._user_auth_token, "login_app_id": self._login_app_id})
            await db.execute(
                "INSERT INTO streaming_auth (service, token_data, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT (service) DO UPDATE SET token_data = EXCLUDED.token_data, updated_at = CURRENT_TIMESTAMP",
                ("qobuz", token_data),
            )
            await db.commit()
            logger.info("qobuz_auth_saved")
        except Exception:
            logger.exception("qobuz_save_auth_error")

    async def restore_auth(self, db: Database) -> bool:
        await self._refresh_credentials()
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
                self._login_app_id = data.get("login_app_id") or QOBUZ_LOGIN_APP_ID
                logger.info("qobuz_auth_restored", app_id=(self._login_app_id or "")[:4])
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
        album_artist = album.get("artist", {}) if album else {}
        image = album.get("image", {}) if album else {}
        cover_path = _proxy_cover(image.get("large") or image.get("small") or image.get("thumbnail"))

        # Artist: prefer performer, fallback to album artist
        artist_name = (
            performer.get("name")
            or album_artist.get("name")
            or t.get("composer", {}).get("name")
            or "Unknown"
        )

        return Track(
            title=t.get("title", "Unknown"),
            artist_name=artist_name,
            album_title=album.get("title"),
            track_number=t.get("track_number", 0),
            disc_number=t.get("media_number", 1),
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
        cover_path = _proxy_cover(image.get("large") or image.get("small") or image.get("thumbnail"))
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
            image_path=_proxy_cover(ar.get("image", {}).get("large") if isinstance(ar.get("image"), dict) else None),
        )

    # ------------------------------------------------------------------
    # Playlist write operations
    # ------------------------------------------------------------------

    @property
    def supports_playlist_write(self) -> bool:
        return True

    async def create_playlist(self, name: str, description: str | None = None) -> str | None:
        try:
            params = {
                "name": name,
                "is_public": "false",
            }
            if description:
                params["description"] = description
            data = await self._api_get("playlist/create", params)
            pid = str(data.get("id", ""))
            logger.info("qobuz_playlist_created", name=name, id=pid)
            return pid
        except Exception:
            logger.exception("qobuz_create_playlist_error")
            return None

    async def add_tracks_to_playlist(self, playlist_id: str, track_ids: list[str]) -> int:
        try:
            ids_str = ",".join(track_ids)
            await self._api_get("playlist/addTracks", {
                "playlist_id": playlist_id,
                "track_ids": ids_str,
            })
            logger.info("qobuz_tracks_added", playlist_id=playlist_id, count=len(track_ids))
            return len(track_ids)
        except Exception:
            logger.exception("qobuz_add_tracks_error")
            return 0

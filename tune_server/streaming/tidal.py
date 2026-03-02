from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Optional

import structlog

from tune_server.config import settings
from tune_server.models import Album, Artist, AudioFormat, FeaturedSection, SearchResult, Source, StreamingPlaylist, Track
from tune_server.streaming.base import StreamingService
from tune_server.streaming.cache import StreamUrlCache

if TYPE_CHECKING:
    from tune_server.db.engine import Database

logger = structlog.get_logger()


class TidalService(StreamingService):
    """Tidal streaming service integration using tidalapi."""

    def __init__(self) -> None:
        self._session = None
        self._url_cache = StreamUrlCache(ttl_seconds=240)  # Tidal URLs expire ~5min
        self._pending_login = None
        self._pending_session = None
        self._auth_task = None
        self._featured_cache: dict[str, object] = {}  # section_id -> PageCategory

    def _make_session(self):
        """Create a tidalapi Session with configured quality."""
        import tidalapi
        quality_map = {
            "LOW": tidalapi.Quality.low_96k,
            "HIGH": tidalapi.Quality.low_320k,
            "LOSSLESS": tidalapi.Quality.high_lossless,
            "HI_RES_LOSSLESS": tidalapi.Quality.hi_res_lossless,
        }
        config = tidalapi.Config(quality=quality_map.get(settings.tidal_quality, tidalapi.Quality.high_lossless))
        return tidalapi.Session(config)

    async def _get_track_url(self, track) -> Optional[str]:
        """Get stream URL with quality fallback."""
        import tidalapi
        # Try configured quality first, then fall back to lower qualities
        fallback_order = [
            tidalapi.Quality.hi_res_lossless,
            tidalapi.Quality.high_lossless,
            tidalapi.Quality.low_320k,
        ]
        # Start from the configured quality level
        quality_map = {
            "LOW": 2,
            "HIGH": 2,
            "LOSSLESS": 1,
            "HI_RES_LOSSLESS": 0,
        }
        start_idx = quality_map.get(settings.tidal_quality, 1)
        for quality in fallback_order[start_idx:]:
            try:
                self._session.audio_quality = quality
                url = await asyncio.wait_for(
                    asyncio.to_thread(track.get_url), timeout=30
                )
                if url:
                    return url
            except Exception:
                logger.debug("tidal_quality_fallback", quality=quality)
                continue
        return None

    @property
    def name(self) -> str:
        return "tidal"

    @property
    def is_authenticated(self) -> bool:
        return self._session is not None and self._session.check_login()

    async def _ensure_authenticated(self) -> bool:
        """Check session validity and refresh if needed."""
        if not self._session:
            return False
        try:
            valid = await asyncio.wait_for(
                asyncio.to_thread(self._session.check_login), timeout=30
            )
            if valid:
                return True
            logger.info("tidal_token_refreshing")
            refreshed = await asyncio.wait_for(
                asyncio.to_thread(
                    self._session.token_refresh, self._session.refresh_token
                ),
                timeout=30,
            )
            if refreshed and self._session.check_login():
                logger.info("tidal_token_refreshed")
                return True
        except asyncio.TimeoutError:
            logger.warning("tidal_refresh_timeout")
        except Exception:
            logger.exception("tidal_refresh_error")
        return False

    @property
    def verification_url(self) -> str | None:
        if self._pending_login:
            return self._pending_login.verification_uri_complete
        return None

    async def authenticate(self, **kwargs) -> bool:
        db = kwargs.get("db")
        try:
            session = self._make_session()

            # OAuth device flow
            login, future = session.login_oauth()

            logger.info(
                "tidal_auth_started",
                verification_url=login.verification_uri_complete,
            )

            # Store pending state and launch background wait
            self._pending_login = login
            self._pending_session = session
            self._auth_task = asyncio.create_task(
                self._wait_for_oauth(session, future, db)
            )

            return False  # not yet authenticated

        except ImportError:
            logger.warning("tidalapi_not_installed")
            return False
        except Exception:
            logger.exception("tidal_auth_error")
            return False

    async def _wait_for_oauth(self, session, future, db) -> None:
        try:
            await asyncio.to_thread(future.result, 300)
            if session.check_login():
                self._session = session
                logger.info(
                    "tidal_authenticated",
                    user=session.user.first_name if session.user else "unknown",
                )
                if db:
                    await self.save_auth(db)
            else:
                logger.warning("tidal_auth_failed")
        except Exception:
            logger.exception("tidal_oauth_wait_error")
        finally:
            self._pending_login = None
            self._pending_session = None
            self._auth_task = None

    async def search(self, query: str, limit: int = 50) -> SearchResult:
        if not await self._ensure_authenticated():
            return SearchResult()

        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(self._session.search, query, limit=limit),
                timeout=30,
            )

            tracks = []
            for t in results.get("tracks", [])[:limit]:
                tracks.append(self._map_track(t))

            albums = []
            for a in results.get("albums", [])[:limit]:
                albums.append(self._map_album(a))

            artists = []
            for ar in results.get("artists", [])[:limit]:
                artists.append(self._map_artist(ar))

            return SearchResult(tracks=tracks, albums=albums, artists=artists)

        except Exception:
            logger.exception("tidal_search_error")
            return SearchResult()

    async def get_track(self, track_id: str) -> Optional[Track]:
        if not await self._ensure_authenticated():
            return None
        try:
            t = await asyncio.wait_for(
                asyncio.to_thread(self._session.track, int(track_id)), timeout=30
            )
            return self._map_track(t)
        except Exception:
            logger.exception("tidal_get_track_error", track_id=track_id)
            return None

    async def get_album(self, album_id: str) -> Optional[Album]:
        if not await self._ensure_authenticated():
            return None
        try:
            a = await asyncio.wait_for(
                asyncio.to_thread(self._session.album, int(album_id)), timeout=30
            )
            return self._map_album(a)
        except Exception:
            logger.exception("tidal_get_album_error", album_id=album_id)
            return None

    async def get_album_tracks(self, album_id: str) -> list[Track]:
        if not await self._ensure_authenticated():
            return []
        try:
            album = await asyncio.wait_for(
                asyncio.to_thread(self._session.album, int(album_id)), timeout=30
            )
            tidal_tracks = await asyncio.wait_for(
                asyncio.to_thread(album.tracks), timeout=30
            )
            return [self._map_track(t) for t in tidal_tracks]
        except Exception:
            logger.exception("tidal_album_tracks_error", album_id=album_id)
            return []

    async def get_artist(self, artist_id: str) -> Optional[Artist]:
        if not await self._ensure_authenticated():
            return None
        try:
            ar = await asyncio.wait_for(
                asyncio.to_thread(self._session.artist, int(artist_id)), timeout=30
            )
            return self._map_artist(ar)
        except Exception:
            return None

    async def get_artist_albums(self, artist_id: str) -> list[Album]:
        if not await self._ensure_authenticated():
            return []
        try:
            artist = await asyncio.wait_for(
                asyncio.to_thread(self._session.artist, int(artist_id)), timeout=30
            )
            albums = await asyncio.wait_for(
                asyncio.to_thread(artist.get_albums), timeout=30
            )
            return [self._map_album(a) for a in albums]
        except Exception:
            logger.exception("tidal_artist_albums_error", artist_id=artist_id)
            return []

    async def get_artist_tracks(self, artist_id: str) -> list[Track]:
        if not await self._ensure_authenticated():
            return []
        try:
            artist = await asyncio.wait_for(
                asyncio.to_thread(self._session.artist, int(artist_id)), timeout=30
            )
            tracks = await asyncio.wait_for(
                asyncio.to_thread(artist.get_top_tracks), timeout=30
            )
            return [self._map_track(t) for t in tracks]
        except Exception:
            logger.exception("tidal_artist_tracks_error", artist_id=artist_id)
            return []

    async def get_stream_url(self, track_id: str) -> Optional[str]:
        cached = self._url_cache.get(track_id)
        if cached:
            return cached

        if not await self._ensure_authenticated():
            return None

        try:
            track = await asyncio.wait_for(
                asyncio.to_thread(self._session.track, int(track_id)), timeout=30
            )
            url = await self._get_track_url(track)
            if url:
                self._url_cache.set(track_id, url)
            return url
        except Exception:
            logger.exception("tidal_stream_url_error", track_id=track_id)
            return None

    async def save_auth(self, db: Database) -> None:
        if not self._session:
            return
        try:
            token_data = json.dumps({
                "session_id": self._session.session_id,
                "token_type": self._session.token_type,
                "access_token": self._session.access_token,
                "refresh_token": self._session.refresh_token,
            })
            await db.execute(
                "INSERT OR REPLACE INTO streaming_auth (service, token_data, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                ("tidal", token_data),
            )
            await db.commit()
            logger.info("tidal_auth_saved")
        except Exception:
            logger.exception("tidal_save_auth_error")

    async def restore_auth(self, db: Database) -> bool:
        try:
            row = await db.fetchone(
                "SELECT token_data FROM streaming_auth WHERE service = ?", ("tidal",)
            )
            if not row:
                return False

            data = json.loads(row["token_data"])
            session = self._make_session()
            session.load_oauth_session(
                data.get("token_type", "Bearer"),
                data.get("access_token", ""),
                data.get("refresh_token", ""),
            )

            if session.check_login():
                self._session = session
                logger.info("tidal_auth_restored")
                return True

            logger.warning("tidal_auth_restore_expired")
            return False
        except ImportError:
            logger.warning("tidalapi_not_installed")
            return False
        except Exception:
            logger.exception("tidal_restore_auth_error")
            return False

    async def get_featured_sections(self) -> list[FeaturedSection]:
        if not await self._ensure_authenticated():
            return []
        try:
            import tidalapi

            home_page = await asyncio.wait_for(
                asyncio.to_thread(self._session.home), timeout=30
            )
            sections = []
            self._featured_cache = {}
            for i, cat in enumerate(home_page.categories):
                title = getattr(cat, "title", None)
                if not title:
                    continue
                items = getattr(cat, "items", [])
                has_albums = any(isinstance(item, tidalapi.Album) for item in items)
                if has_albums:
                    section_id = f"home-{i}"
                    sections.append(FeaturedSection(id=section_id, name=title))
                    self._featured_cache[section_id] = cat
            return sections
        except Exception:
            logger.exception("tidal_featured_sections_error")
            return []

    async def get_featured(self, section: str, limit: int = 20) -> list[Album]:
        cat = self._featured_cache.get(section)
        if not cat:
            return []
        try:
            import tidalapi

            items = getattr(cat, "items", [])
            albums = []
            for item in items:
                if isinstance(item, tidalapi.Album):
                    albums.append(self._map_album(item))
                    if len(albums) >= limit:
                        break
            return albums
        except Exception:
            logger.exception("tidal_featured_error", section=section)
            return []

    async def get_user_playlists(self) -> list[StreamingPlaylist]:
        if not await self._ensure_authenticated():
            return []
        try:
            import tidalapi
            playlists = await asyncio.wait_for(
                asyncio.to_thread(self._session.user.playlist_and_favorite_playlists), timeout=60
            )
            return [self._map_playlist(p) for p in playlists if isinstance(p, tidalapi.Playlist)]
        except Exception:
            logger.exception("tidal_user_playlists_error")
            return []

    async def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        if not await self._ensure_authenticated():
            return []
        try:
            playlist = await asyncio.wait_for(
                asyncio.to_thread(self._session.playlist, playlist_id), timeout=30
            )
            tidal_tracks = await asyncio.wait_for(
                asyncio.to_thread(playlist.tracks), timeout=60
            )
            return [self._map_track(t) for t in tidal_tracks]
        except Exception:
            logger.exception("tidal_playlist_tracks_error", playlist_id=playlist_id)
            return []

    def _map_playlist(self, p) -> StreamingPlaylist:
        cover_path = None
        try:
            cover_path = p.image(640)
        except Exception:
            pass
        return StreamingPlaylist(
            source_id=str(p.id),
            name=p.name or "Unknown",
            description=getattr(p, "description", None),
            track_count=getattr(p, "num_tracks", 0) or 0,
            duration_ms=int(getattr(p, "duration", 0) or 0) * 1000,
            cover_path=cover_path,
            source=Source.TIDAL,
        )

    async def disconnect(self, db: Database) -> None:
        self._session = None
        self._featured_cache = {}
        try:
            await db.execute(
                "DELETE FROM streaming_auth WHERE service = ?", ("tidal",)
            )
            await db.commit()
            logger.info("tidal_disconnected")
        except Exception:
            logger.exception("tidal_disconnect_error")

    async def close(self) -> None:
        self._session = None

    def _map_track(self, t) -> Track:
        duration = int(t.duration * 1000) if t.duration else 0
        artist_name = t.artist.name if t.artist else "Unknown"
        album_title = t.album.name if t.album else None

        cover_path = None
        if t.album:
            try:
                cover_path = t.album.image(640)
            except Exception:
                pass

        quality = getattr(t, "audio_quality", None)
        fmt = AudioFormat.FLAC if quality in ("LOSSLESS", "HI_RES", "HI_RES_LOSSLESS") else AudioFormat.AAC

        return Track(
            title=t.name or "Unknown",
            artist_name=artist_name,
            album_title=album_title,
            duration_ms=duration,
            format=fmt,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
            cover_path=cover_path,
            source=Source.TIDAL,
            source_id=str(t.id),
        )

    def _map_album(self, a) -> Album:
        cover_path = None
        try:
            cover_path = a.image(640)
        except Exception:
            pass
        return Album(
            title=a.name or "Unknown",
            artist_name=a.artist.name if a.artist else "Unknown",
            year=getattr(a, "year", None),
            track_count=getattr(a, "num_tracks", 0) or 0,
            cover_path=cover_path,
            source=Source.TIDAL,
            source_id=str(a.id),
        )

    def _map_artist(self, ar) -> Artist:
        return Artist(
            name=ar.name or "Unknown",
            source=Source.TIDAL,
        )

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
        self._auth_expires_at: float | None = None  # monotonic timestamp
        self._auth_error: str | None = None
        self._featured_cache: dict[str, object] = {}  # section_id -> PageCategory
        self._featured_sections_cache: list | None = None
        self._featured_cache_time: float = 0
        self._featured_cache_ttl: float = 1800  # 30 minutes

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

    @property
    def auth_expires_at(self) -> float | None:
        """Timestamp (monotonic) when the current device code expires."""
        return self._auth_expires_at

    @property
    def auth_remaining_seconds(self) -> int | None:
        """Seconds remaining before the current device code expires, or None."""
        if self._auth_expires_at is None:
            return None
        import time
        remaining = int(self._auth_expires_at - time.monotonic())
        return max(0, remaining)

    async def authenticate(self, **kwargs) -> bool:
        db = kwargs.get("db")
        try:
            session = self._make_session()

            # OAuth device flow
            login, future = session.login_oauth()

            # tidalapi device codes expire after ~300s (5 min)
            import time
            expires_in = getattr(login, "expires_in", 300) or 300
            self._auth_expires_at = time.monotonic() + expires_in

            logger.info(
                "tidal_auth_started",
                verification_url=login.verification_uri_complete,
                expires_in=expires_in,
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
        """Wait for the user to complete the OAuth device flow.

        If the first code expires (5 min timeout), automatically generates a
        new code and waits again -- up to 3 total attempts.
        """
        max_attempts = 3
        attempt = 0

        while attempt < max_attempts:
            attempt += 1
            try:
                await asyncio.to_thread(future.result, 300)
                if session.check_login():
                    self._session = session
                    self._auth_error = None
                    self._pending_login = None
                    self._pending_session = None
                    self._auth_task = None
                    self._auth_expires_at = None
                    logger.info(
                        "tidal_authenticated",
                        user=session.user.first_name if session.user else "unknown",
                    )
                    if db:
                        await self.save_auth(db)
                    return
                else:
                    logger.warning("tidal_auth_failed", attempt=attempt)
            except Exception:
                logger.info("tidal_oauth_code_expired", attempt=attempt, max=max_attempts)

            # Code expired -- retry with a fresh code if attempts remain
            if attempt < max_attempts:
                try:
                    import time as _time
                    login, future = session.login_oauth()
                    expires_in = getattr(login, "expires_in", 300) or 300
                    self._auth_expires_at = _time.monotonic() + expires_in
                    self._pending_login = login
                    self._pending_session = session
                    self._auth_error = "Code expire -- un nouveau code a ete genere"
                    logger.info(
                        "tidal_auth_retry",
                        attempt=attempt + 1,
                        verification_url=login.verification_uri_complete,
                        expires_in=expires_in,
                    )
                except Exception:
                    logger.exception("tidal_auth_retry_error")
                    break

        self._auth_error = "Authentification Tidal echouee apres plusieurs tentatives"
        logger.warning("tidal_auth_exhausted", attempts=max_attempts)
        self._pending_login = None
        self._pending_session = None
        self._auth_task = None
        self._auth_expires_at = None

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
        except Exception as e:
            logger.debug("tidal_get_artist_failed", artist_id=artist_id, error=str(e))
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
                "INSERT INTO streaming_auth (service, token_data, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT (service) DO UPDATE SET token_data = EXCLUDED.token_data, updated_at = CURRENT_TIMESTAMP",
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

        import time
        if (
            self._featured_sections_cache is not None
            and (time.monotonic() - self._featured_cache_time) < self._featured_cache_ttl
        ):
            return self._featured_sections_cache

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
            self._featured_sections_cache = sections
            self._featured_cache_time = time.monotonic()
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

    _playlists_cache: list[StreamingPlaylist] | None = None
    _playlists_cache_time: float = 0

    async def get_user_playlists(self) -> list[StreamingPlaylist]:
        # Cache for 5 minutes (Tidal takes ~34s to fetch 280 playlists)
        import time
        if self._playlists_cache is not None and (time.monotonic() - self._playlists_cache_time) < 300:
            logger.debug("tidal_playlists_cache_hit", count=len(self._playlists_cache))
            return self._playlists_cache

        if not await self._ensure_authenticated():
            return []
        try:
            import tidalapi

            def _fetch_all():
                all_playlists = []
                # Fetch user's own playlists
                try:
                    own = self._session.user.playlists()
                    all_playlists.extend([p for p in own if isinstance(p, tidalapi.Playlist)])
                except Exception as e:
                    logger.debug("tidal_own_playlists_fetch_failed", error=str(e))
                # Fetch favorite playlists (paginated)
                try:
                    favs = self._session.user.favorites
                    offset = 0
                    while True:
                        batch = favs.playlists(limit=100, offset=offset)
                        if not batch:
                            break
                        all_playlists.extend([p for p in batch if isinstance(p, tidalapi.Playlist)])
                        if len(batch) < 100:
                            break
                        offset += 100
                except Exception as e:
                    logger.debug("tidal_favorite_playlists_fetch_failed", error=str(e))
                # Deduplicate by ID
                seen = set()
                unique = []
                for p in all_playlists:
                    if p.id not in seen:
                        seen.add(p.id)
                        unique.append(p)
                return unique

            playlists = await asyncio.wait_for(
                asyncio.to_thread(_fetch_all), timeout=120
            )
            result = [self._map_playlist(p) for p in playlists]
            self._playlists_cache = result
            self._playlists_cache_time = time.monotonic()
            logger.info("tidal_playlists_loaded", count=len(result))
            return result
        except Exception:
            logger.exception("tidal_user_playlists_error")
            return self._playlists_cache or []

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
        except Exception as e:
            logger.debug("tidal_playlist_image_failed", error=str(e))
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
        self._featured_sections_cache = None
        self._featured_cache_time = 0
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
            except Exception as e:
                logger.debug("tidal_track_cover_failed", error=str(e))

        # Track.audio_quality ment (retourne toujours 'LOSSLESS' sur tidalapi
        # 0.8.x). La vraie qualité est dans media_metadata_tags qui liste les
        # niveaux dispo : ['LOSSLESS'] = CD 16/44, ['LOSSLESS', 'HIRES_LOSSLESS']
        # = HiRes FLAC 24-bit (sample_rate variable 96k/192k, on prend 96k par
        # défaut — la vraie valeur arrive via Stream.audio_quality au moment du
        # get_stream() lors de la lecture).
        tags = set(getattr(t, "media_metadata_tags", []) or [])
        if "HIRES_LOSSLESS" in tags:
            fmt, sample_rate, bit_depth = AudioFormat.FLAC, 96000, 24
        elif "LOSSLESS" in tags:
            fmt, sample_rate, bit_depth = AudioFormat.FLAC, 44100, 16
        else:
            fmt, sample_rate, bit_depth = AudioFormat.AAC, 44100, 16

        return Track(
            title=t.name or "Unknown",
            artist_name=artist_name,
            album_title=album_title,
            track_number=getattr(t, "track_num", 0) or 0,
            disc_number=getattr(t, "volume_num", 1) or 1,
            duration_ms=duration,
            format=fmt,
            sample_rate=sample_rate,
            bit_depth=bit_depth,
            channels=2,
            cover_path=cover_path,
            source=Source.TIDAL,
            source_id=str(t.id),
        )

    def _map_album(self, a) -> Album:
        cover_path = None
        try:
            cover_path = a.image(640)
        except Exception as e:
            logger.debug("tidal_album_cover_failed", error=str(e))
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
            source_id=str(ar.id),
        )

    # ------------------------------------------------------------------
    # Playlist write operations
    # ------------------------------------------------------------------

    @property
    def supports_playlist_write(self) -> bool:
        return True

    async def create_playlist(self, name: str, description: str | None = None) -> str | None:
        if not await self._ensure_authenticated():
            return None
        try:
            playlist = await asyncio.to_thread(
                self._session.user.create_playlist, name, description or ""
            )
            logger.info("tidal_playlist_created", name=name, id=playlist.id)
            return str(playlist.id)
        except Exception:
            logger.exception("tidal_create_playlist_error")
            return None

    async def add_tracks_to_playlist(self, playlist_id: str, track_ids: list[str]) -> int:
        if not await self._ensure_authenticated():
            return 0
        try:
            playlist = await asyncio.to_thread(self._session.playlist, playlist_id)
            # tidalapi expects list of track IDs as ints
            int_ids = [int(tid) for tid in track_ids if tid.isdigit()]
            if not int_ids:
                return 0
            await asyncio.to_thread(playlist.add, int_ids)
            logger.info("tidal_tracks_added", playlist_id=playlist_id, count=len(int_ids))
            return len(int_ids)
        except Exception:
            logger.exception("tidal_add_tracks_error")
            return 0

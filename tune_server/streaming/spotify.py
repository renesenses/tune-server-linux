from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Optional

import structlog

from tune_server.config import settings
from tune_server.models import (
    Album,
    Artist,
    AudioFormat,
    SearchResult,
    Source,
    StreamingPlaylist,
    Track,
)
from tune_server.streaming.base import StreamingService

if TYPE_CHECKING:
    from tune_server.db.engine import Database

logger = structlog.get_logger()

SCOPES = "user-read-private user-library-read playlist-read-private playlist-read-collaborative"


class SpotifyService(StreamingService):
    """Spotify streaming service integration using spotipy (PKCE auth)."""

    def __init__(self) -> None:
        self._sp = None  # spotipy.Spotify client
        self._auth_manager = None
        self._verification_url: str | None = None
        self._authenticated = False

    @property
    def name(self) -> str:
        return "spotify"

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated and self._sp is not None

    @property
    def verification_url(self) -> str | None:
        return self._verification_url

    async def authenticate(self, **kwargs) -> bool:
        db = kwargs.get("db")
        try:
            import spotipy

            cache_path = ".spotify_cache"
            self._auth_manager = spotipy.SpotifyPKCE(
                client_id=settings.spotify_client_id,
                redirect_uri=settings.spotify_redirect_uri,
                scope=SCOPES,
                cache_path=cache_path,
                open_browser=False,
            )

            auth_url = self._auth_manager.get_authorize_url()
            self._verification_url = auth_url
            logger.info("spotify_auth_started", verification_url=auth_url)

            return False  # not yet authenticated, waiting for callback

        except ImportError:
            logger.warning("spotipy_not_installed")
            return False
        except Exception:
            logger.exception("spotify_auth_error")
            return False

    async def complete_auth(self, code: str, db: Database | None = None) -> bool:
        """Called by the callback route after Spotify redirects with a code."""
        try:
            token_info = await asyncio.to_thread(
                self._auth_manager.get_access_token, code
            )
            if not token_info:
                logger.warning("spotify_auth_no_token")
                return False

            import spotipy

            self._sp = spotipy.Spotify(auth_manager=self._auth_manager)
            self._authenticated = True
            self._verification_url = None

            # Verify by fetching current user
            user = await asyncio.to_thread(self._sp.current_user)
            logger.info(
                "spotify_authenticated",
                user=user.get("display_name", "unknown") if user else "unknown",
            )

            if db:
                await self.save_auth(db)

            return True
        except Exception:
            logger.exception("spotify_complete_auth_error")
            self._verification_url = None
            return False

    async def search(self, query: str, limit: int = 50) -> SearchResult:
        if not self.is_authenticated:
            return SearchResult()
        try:
            results = await asyncio.to_thread(
                self._sp.search, q=query, limit=min(limit, 50), type="track,album,artist"
            )
            tracks = [
                self._map_track(t)
                for t in (results.get("tracks", {}).get("items") or [])
            ]
            albums = [
                self._map_album(a)
                for a in (results.get("albums", {}).get("items") or [])
            ]
            artists = [
                self._map_artist(ar)
                for ar in (results.get("artists", {}).get("items") or [])
            ]
            return SearchResult(tracks=tracks, albums=albums, artists=artists)
        except Exception:
            logger.exception("spotify_search_error")
            return SearchResult()

    async def get_track(self, track_id: str) -> Optional[Track]:
        if not self.is_authenticated:
            return None
        try:
            t = await asyncio.to_thread(self._sp.track, track_id)
            return self._map_track(t) if t else None
        except Exception:
            logger.exception("spotify_get_track_error", track_id=track_id)
            return None

    async def get_album(self, album_id: str) -> Optional[Album]:
        if not self.is_authenticated:
            return None
        try:
            a = await asyncio.to_thread(self._sp.album, album_id)
            return self._map_album(a) if a else None
        except Exception:
            logger.exception("spotify_get_album_error", album_id=album_id)
            return None

    async def get_album_tracks(self, album_id: str) -> list[Track]:
        if not self.is_authenticated:
            return []
        try:
            album = await asyncio.to_thread(self._sp.album, album_id)
            if not album:
                return []
            album_name = album.get("name")
            album_images = album.get("images") or []
            album_cover = album_images[0]["url"] if album_images else None
            album_artists = album.get("artists") or []
            album_artist_name = album_artists[0]["name"] if album_artists else None

            tracks = []
            results = album.get("tracks") or {}
            items = results.get("items") or []
            while True:
                for t in items:
                    tracks.append(self._map_album_track(
                        t, album_name=album_name, album_cover=album_cover,
                        album_artist_name=album_artist_name,
                    ))
                if results.get("next"):
                    results = await asyncio.to_thread(self._sp.next, results)
                    items = results.get("items") or [] if results else []
                else:
                    break
            return tracks
        except Exception:
            logger.exception("spotify_album_tracks_error", album_id=album_id)
            return []

    async def get_artist(self, artist_id: str) -> Optional[Artist]:
        if not self.is_authenticated:
            return None
        try:
            ar = await asyncio.to_thread(self._sp.artist, artist_id)
            return self._map_artist(ar) if ar else None
        except Exception:
            logger.exception("spotify_get_artist_error", artist_id=artist_id)
            return None

    async def get_artist_albums(self, artist_id: str) -> list[Album]:
        if not self.is_authenticated:
            return []
        try:
            results = await asyncio.to_thread(
                self._sp.artist_albums, artist_id, album_type="album,single", limit=50
            )
            return [self._map_album(a) for a in (results.get("items") or [])]
        except Exception:
            logger.exception("spotify_artist_albums_error", artist_id=artist_id)
            return []

    async def get_artist_tracks(self, artist_id: str) -> list[Track]:
        if not self.is_authenticated:
            return []
        try:
            results = await asyncio.to_thread(
                self._sp.artist_top_tracks, artist_id
            )
            return [self._map_track(t) for t in (results.get("tracks") or [])]
        except Exception:
            logger.exception("spotify_artist_tracks_error", artist_id=artist_id)
            return []

    async def get_stream_url(self, track_id: str) -> Optional[str]:
        """Return preview URL (30s MP3) or None. Spotify API has no full stream URLs."""
        if not self.is_authenticated:
            return None
        try:
            t = await asyncio.to_thread(self._sp.track, track_id)
            return t.get("preview_url") if t else None
        except Exception:
            logger.exception("spotify_stream_url_error", track_id=track_id)
            return None

    async def get_user_playlists(self) -> list[StreamingPlaylist]:
        if not self.is_authenticated:
            return []
        try:
            playlists = []
            results = await asyncio.to_thread(self._sp.current_user_playlists, limit=50)
            while results:
                for p in results.get("items") or []:
                    playlists.append(self._map_playlist(p))
                if results.get("next"):
                    results = await asyncio.to_thread(self._sp.next, results)
                else:
                    break
            return playlists
        except Exception:
            logger.exception("spotify_user_playlists_error")
            return []

    async def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        if not self.is_authenticated:
            return []
        try:
            tracks = []
            results = await asyncio.to_thread(
                self._sp.playlist_tracks, playlist_id, limit=100
            )
            while results:
                for item in results.get("items") or []:
                    t = item.get("track")
                    if t and t.get("id"):
                        tracks.append(self._map_track(t))
                if results.get("next"):
                    results = await asyncio.to_thread(self._sp.next, results)
                else:
                    break
            return tracks
        except Exception:
            logger.exception("spotify_playlist_tracks_error", playlist_id=playlist_id)
            return []

    async def save_auth(self, db: Database) -> None:
        if not self._auth_manager:
            return
        try:
            token_info = self._auth_manager.cache_handler.get_cached_token()
            if not token_info:
                return
            token_data = json.dumps(token_info)
            await db.execute(
                "INSERT OR REPLACE INTO streaming_auth (service, token_data, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                ("spotify", token_data),
            )
            await db.commit()
            logger.info("spotify_auth_saved")
        except Exception:
            logger.exception("spotify_save_auth_error")

    async def restore_auth(self, db: Database) -> bool:
        try:
            row = await db.fetchone(
                "SELECT token_data FROM streaming_auth WHERE service = ?", ("spotify",)
            )
            if not row:
                return False

            token_info = json.loads(row["token_data"])

            import spotipy
            from spotipy.cache_handler import MemoryCacheHandler

            cache_handler = MemoryCacheHandler(token_info=token_info)
            self._auth_manager = spotipy.SpotifyPKCE(
                client_id=settings.spotify_client_id,
                redirect_uri=settings.spotify_redirect_uri,
                scope=SCOPES,
                cache_handler=cache_handler,
                open_browser=False,
            )

            self._sp = spotipy.Spotify(auth_manager=self._auth_manager)

            # Verify the token works
            user = await asyncio.to_thread(self._sp.current_user)
            if user:
                self._authenticated = True
                logger.info("spotify_auth_restored", user=user.get("display_name", "unknown"))
                return True

            logger.warning("spotify_auth_restore_expired")
            self._sp = None
            self._auth_manager = None
            return False
        except ImportError:
            logger.warning("spotipy_not_installed")
            return False
        except Exception:
            logger.exception("spotify_restore_auth_error")
            self._sp = None
            self._auth_manager = None
            return False

    async def disconnect(self, db: Database) -> None:
        self._sp = None
        self._auth_manager = None
        self._authenticated = False
        self._verification_url = None
        try:
            await db.execute(
                "DELETE FROM streaming_auth WHERE service = ?", ("spotify",)
            )
            await db.commit()
            logger.info("spotify_disconnected")
        except Exception:
            logger.exception("spotify_disconnect_error")

    async def close(self) -> None:
        self._sp = None
        self._auth_manager = None
        self._authenticated = False

    # --- Mapping helpers ---

    def _map_track(self, t: dict) -> Track:
        duration = t.get("duration_ms") or 0
        artists = t.get("artists") or []
        artist_name = artists[0]["name"] if artists else "Unknown"
        album = t.get("album") or {}
        album_title = album.get("name")
        images = album.get("images") or []
        cover_path = images[0]["url"] if images else None

        return Track(
            title=t.get("name") or "Unknown",
            artist_name=artist_name,
            album_title=album_title,
            duration_ms=duration,
            format=AudioFormat.OGG,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
            cover_path=cover_path,
            source=Source.SPOTIFY,
            source_id=t.get("id") or "",
        )

    def _map_album_track(
        self, t: dict, album_name: str | None = None,
        album_cover: str | None = None, album_artist_name: str | None = None,
    ) -> Track:
        """Map a simplified track object from album.tracks (no album info embedded)."""
        duration = t.get("duration_ms") or 0
        artists = t.get("artists") or []
        artist_name = artists[0]["name"] if artists else (album_artist_name or "Unknown")

        return Track(
            title=t.get("name") or "Unknown",
            artist_name=artist_name,
            album_title=album_name,
            track_number=t.get("track_number") or 0,
            disc_number=t.get("disc_number") or 1,
            duration_ms=duration,
            format=AudioFormat.OGG,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
            cover_path=album_cover,
            source=Source.SPOTIFY,
            source_id=t.get("id") or "",
        )

    def _map_album(self, a: dict) -> Album:
        images = a.get("images") or []
        cover_path = images[0]["url"] if images else None
        artists = a.get("artists") or []
        artist_name = artists[0]["name"] if artists else "Unknown"
        release_date = a.get("release_date") or ""
        year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None

        return Album(
            title=a.get("name") or "Unknown",
            artist_name=artist_name,
            year=year,
            track_count=a.get("total_tracks") or 0,
            cover_path=cover_path,
            source=Source.SPOTIFY,
            source_id=a.get("id") or "",
        )

    def _map_artist(self, ar: dict) -> Artist:
        images = ar.get("images") or []
        image_path = images[0]["url"] if images else None
        return Artist(
            name=ar.get("name") or "Unknown",
            image_path=image_path,
        )

    def _map_playlist(self, p: dict) -> StreamingPlaylist:
        images = p.get("images") or []
        cover_path = images[0]["url"] if images else None
        tracks_info = p.get("tracks") or {}
        return StreamingPlaylist(
            source_id=p.get("id") or "",
            name=p.get("name") or "Unknown",
            description=p.get("description"),
            track_count=tracks_info.get("total") or 0,
            duration_ms=0,
            cover_path=cover_path,
            source=Source.SPOTIFY,
        )

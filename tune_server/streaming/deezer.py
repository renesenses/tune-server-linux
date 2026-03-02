from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlencode

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

DEEZER_PERMISSIONS = "basic_access,manage_library,listening_history"
DEEZER_AUTH_URL = "https://connect.deezer.com/oauth/auth.php"
DEEZER_TOKEN_URL = "https://connect.deezer.com/oauth/access_token.php"


class DeezerService(StreamingService):
    """Deezer streaming service integration using deezer-python (OAuth 2.0 standard)."""

    def __init__(self) -> None:
        self._client = None  # deezer.Client
        self._access_token: str | None = None
        self._verification_url: str | None = None
        self._authenticated = False

    @property
    def name(self) -> str:
        return "deezer"

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated and self._client is not None

    @property
    def verification_url(self) -> str | None:
        return self._verification_url

    async def authenticate(self, **kwargs) -> bool:
        try:
            params = urlencode({
                "app_id": settings.deezer_app_id,
                "redirect_uri": settings.deezer_redirect_uri,
                "perms": DEEZER_PERMISSIONS,
            })
            auth_url = f"{DEEZER_AUTH_URL}?{params}"
            self._verification_url = auth_url
            logger.info("deezer_auth_started", verification_url=auth_url)
            return False  # waiting for callback
        except Exception:
            logger.exception("deezer_auth_error")
            return False

    async def complete_auth(self, code: str, db: Database | None = None) -> bool:
        """Called by the callback route after Deezer redirects with a code."""
        try:
            import aiohttp

            params = {
                "app_id": settings.deezer_app_id,
                "secret": settings.deezer_app_secret,
                "code": code,
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(DEEZER_TOKEN_URL, params=params) as resp:
                    text = await resp.text()

            # Deezer returns "access_token=...&expires=..." as form-encoded
            token_data = dict(pair.split("=") for pair in text.split("&") if "=" in pair)
            access_token = token_data.get("access_token")

            if not access_token or access_token == "wrong code":
                logger.warning("deezer_auth_no_token", response=text)
                self._verification_url = None
                return False

            self._access_token = access_token
            self._init_client(access_token)
            self._authenticated = True
            self._verification_url = None

            # Verify by fetching current user
            import deezer
            user = await asyncio.to_thread(self._client.get_user)
            logger.info(
                "deezer_authenticated",
                user=user.name if user else "unknown",
            )

            if db:
                await self.save_auth(db)

            return True
        except Exception:
            logger.exception("deezer_complete_auth_error")
            self._verification_url = None
            return False

    def _init_client(self, access_token: str) -> None:
        import deezer
        self._client = deezer.Client(access_token=access_token)

    async def search(self, query: str, limit: int = 50) -> SearchResult:
        if not self.is_authenticated:
            return SearchResult()
        try:
            raw_tracks = await asyncio.to_thread(
                self._client.search, query=query
            )
            raw_albums = await asyncio.to_thread(
                self._client.search_albums, query=query
            )
            raw_artists = await asyncio.to_thread(
                self._client.search_artists, query=query
            )
            tracks = [self._map_track(t) for t in raw_tracks[:limit]]
            albums = [self._map_album(a) for a in raw_albums[:limit]]
            artists = [self._map_artist(ar) for ar in raw_artists[:limit]]
            return SearchResult(tracks=tracks, albums=albums, artists=artists)
        except Exception:
            logger.exception("deezer_search_error")
            return SearchResult()

    async def get_track(self, track_id: str) -> Optional[Track]:
        if not self.is_authenticated:
            return None
        try:
            t = await asyncio.to_thread(self._client.get_track, int(track_id))
            return self._map_track(t) if t else None
        except Exception:
            logger.exception("deezer_get_track_error", track_id=track_id)
            return None

    async def get_album(self, album_id: str) -> Optional[Album]:
        if not self.is_authenticated:
            return None
        try:
            a = await asyncio.to_thread(self._client.get_album, int(album_id))
            return self._map_album(a) if a else None
        except Exception:
            logger.exception("deezer_get_album_error", album_id=album_id)
            return None

    async def get_album_tracks(self, album_id: str) -> list[Track]:
        if not self.is_authenticated:
            return []
        try:
            album = await asyncio.to_thread(self._client.get_album, int(album_id))
            if not album:
                return []
            raw_tracks = await asyncio.to_thread(album.get_tracks)
            return [
                self._map_album_track(t, album)
                for t in raw_tracks
            ]
        except Exception:
            logger.exception("deezer_album_tracks_error", album_id=album_id)
            return []

    async def get_artist(self, artist_id: str) -> Optional[Artist]:
        if not self.is_authenticated:
            return None
        try:
            ar = await asyncio.to_thread(self._client.get_artist, int(artist_id))
            return self._map_artist(ar) if ar else None
        except Exception:
            logger.exception("deezer_get_artist_error", artist_id=artist_id)
            return None

    async def get_artist_albums(self, artist_id: str) -> list[Album]:
        if not self.is_authenticated:
            return []
        try:
            artist = await asyncio.to_thread(self._client.get_artist, int(artist_id))
            if not artist:
                return []
            raw_albums = await asyncio.to_thread(artist.get_albums)
            return [self._map_album(a) for a in raw_albums]
        except Exception:
            logger.exception("deezer_artist_albums_error", artist_id=artist_id)
            return []

    async def get_artist_tracks(self, artist_id: str) -> list[Track]:
        if not self.is_authenticated:
            return []
        try:
            artist = await asyncio.to_thread(self._client.get_artist, int(artist_id))
            if not artist:
                return []
            raw_tracks = await asyncio.to_thread(artist.get_top)
            return [self._map_track(t) for t in raw_tracks]
        except Exception:
            logger.exception("deezer_artist_tracks_error", artist_id=artist_id)
            return []

    async def get_stream_url(self, track_id: str) -> Optional[str]:
        """Return preview URL (30s MP3). Deezer API has no full stream URLs."""
        if not self.is_authenticated:
            return None
        try:
            t = await asyncio.to_thread(self._client.get_track, int(track_id))
            return t.preview if t else None
        except Exception:
            logger.exception("deezer_stream_url_error", track_id=track_id)
            return None

    async def get_user_playlists(self) -> list[StreamingPlaylist]:
        if not self.is_authenticated:
            return []
        try:
            user = await asyncio.to_thread(self._client.get_user)
            if not user:
                return []
            raw_playlists = await asyncio.to_thread(user.get_playlists)
            return [self._map_playlist(p) for p in raw_playlists]
        except Exception:
            logger.exception("deezer_user_playlists_error")
            return []

    async def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        if not self.is_authenticated:
            return []
        try:
            playlist = await asyncio.to_thread(
                self._client.get_playlist, int(playlist_id)
            )
            if not playlist:
                return []
            raw_tracks = await asyncio.to_thread(playlist.get_tracks)
            return [self._map_track(t) for t in raw_tracks]
        except Exception:
            logger.exception("deezer_playlist_tracks_error", playlist_id=playlist_id)
            return []

    async def save_auth(self, db: Database) -> None:
        if not self._access_token:
            return
        try:
            token_data = json.dumps({"access_token": self._access_token})
            await db.execute(
                "INSERT OR REPLACE INTO streaming_auth (service, token_data, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                ("deezer", token_data),
            )
            await db.commit()
            logger.info("deezer_auth_saved")
        except Exception:
            logger.exception("deezer_save_auth_error")

    async def restore_auth(self, db: Database) -> bool:
        try:
            row = await db.fetchone(
                "SELECT token_data FROM streaming_auth WHERE service = ?", ("deezer",)
            )
            if not row:
                return False

            data = json.loads(row["token_data"])
            access_token = data.get("access_token")
            if not access_token:
                return False

            self._access_token = access_token
            self._init_client(access_token)

            # Verify the token works
            user = await asyncio.to_thread(self._client.get_user)
            if user and user.name:
                self._authenticated = True
                logger.info("deezer_auth_restored", user=user.name)
                return True

            logger.warning("deezer_auth_restore_expired")
            self._client = None
            self._access_token = None
            return False
        except ImportError:
            logger.warning("deezer_python_not_installed")
            return False
        except Exception:
            logger.exception("deezer_restore_auth_error")
            self._client = None
            self._access_token = None
            return False

    async def disconnect(self, db: Database) -> None:
        self._client = None
        self._access_token = None
        self._authenticated = False
        self._verification_url = None
        try:
            await db.execute(
                "DELETE FROM streaming_auth WHERE service = ?", ("deezer",)
            )
            await db.commit()
            logger.info("deezer_disconnected")
        except Exception:
            logger.exception("deezer_disconnect_error")

    async def close(self) -> None:
        self._client = None
        self._access_token = None
        self._authenticated = False

    # --- Mapping helpers ---

    def _map_track(self, t) -> Track:
        artist_name = t.artist.name if t.artist else "Unknown"
        album_title = t.album.title if t.album else None
        cover_path = t.album.cover_big if t.album else None

        return Track(
            title=t.title or "Unknown",
            artist_name=artist_name,
            album_title=album_title,
            track_number=getattr(t, "track_position", 0) or 0,
            disc_number=getattr(t, "disk_number", 1) or 1,
            duration_ms=(t.duration or 0) * 1000,
            format=AudioFormat.MP3,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
            cover_path=cover_path,
            source=Source.DEEZER,
            source_id=str(t.id),
        )

    def _map_album_track(self, t, album) -> Track:
        """Map a track from album.get_tracks() with album context."""
        artist_name = t.artist.name if t.artist else (album.artist.name if album.artist else "Unknown")
        cover_path = album.cover_big if album else None

        return Track(
            title=t.title or "Unknown",
            artist_name=artist_name,
            album_title=album.title if album else None,
            track_number=getattr(t, "track_position", 0) or 0,
            disc_number=getattr(t, "disk_number", 1) or 1,
            duration_ms=(t.duration or 0) * 1000,
            format=AudioFormat.MP3,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
            cover_path=cover_path,
            source=Source.DEEZER,
            source_id=str(t.id),
        )

    def _map_album(self, a) -> Album:
        artist_name = a.artist.name if a.artist else "Unknown"
        cover_path = getattr(a, "cover_big", None)
        release_date = getattr(a, "release_date", None)
        year = None
        if release_date:
            date_str = str(release_date)
            if len(date_str) >= 4 and date_str[:4].isdigit():
                year = int(date_str[:4])

        return Album(
            title=a.title or "Unknown",
            artist_name=artist_name,
            year=year,
            track_count=getattr(a, "nb_tracks", 0) or 0,
            cover_path=cover_path,
            source=Source.DEEZER,
            source_id=str(a.id),
        )

    def _map_artist(self, ar) -> Artist:
        image_path = getattr(ar, "picture_big", None)
        return Artist(
            name=ar.name or "Unknown",
            image_path=image_path,
        )

    def _map_playlist(self, p) -> StreamingPlaylist:
        cover_path = getattr(p, "picture_big", None)
        duration_ms = (getattr(p, "duration", 0) or 0) * 1000
        return StreamingPlaylist(
            source_id=str(p.id),
            name=p.title or "Unknown",
            description=getattr(p, "description", None),
            track_count=getattr(p, "nb_tracks", 0) or 0,
            duration_ms=duration_ms,
            cover_path=cover_path,
            source=Source.DEEZER,
        )

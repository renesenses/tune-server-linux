"""Last.fm scrobbling — submit played tracks to Last.fm."""

from __future__ import annotations

import asyncio
import hashlib
import time

import aiohttp
import structlog

logger = structlog.get_logger()

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"

# Last.fm scrobble rules: track must be played for at least 50% of its
# duration *or* 4 minutes, whichever comes first.  Tracks shorter than
# 30 seconds should not be scrobbled at all.
_SCROBBLE_MIN_DURATION_MS = 30_000
_SCROBBLE_MAX_LISTEN_MS = 4 * 60 * 1000  # 4 minutes
_SCROBBLE_PERCENT = 0.50


class LastfmScrobbler:
    """Scrobble tracks to Last.fm using the Scrobble API (2.0)."""

    def __init__(self, api_key: str, api_secret: str, session_key: str | None = None) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._session_key = session_key

    # -- public helpers -------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return bool(self._session_key)

    def set_session_key(self, sk: str | None) -> None:
        self._session_key = sk

    # -- API signature --------------------------------------------------------

    def _sign(self, params: dict) -> str:
        """Generate API signature for signed requests."""
        sig_str = "".join(f"{k}{params[k]}" for k in sorted(params)) + self._api_secret
        return hashlib.md5(sig_str.encode()).hexdigest()

    # -- Auth flow ------------------------------------------------------------

    async def get_auth_token(self) -> str | None:
        """Get an auth token for user authorization flow."""
        params = {"method": "auth.getToken", "api_key": self._api_key, "format": "json"}
        params["api_sig"] = self._sign({k: v for k, v in params.items() if k != "format"})
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(LASTFM_API_URL, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("token")
        except Exception:
            logger.debug("lastfm_get_token_error", exc_info=True)
        return None

    def get_auth_url(self, token: str) -> str:
        """Return the URL for user to authorize the app."""
        return f"https://www.last.fm/api/auth/?api_key={self._api_key}&token={token}"

    async def get_session(self, token: str) -> str | None:
        """Exchange authorized token for a session key."""
        params = {"method": "auth.getSession", "api_key": self._api_key, "token": token}
        params["api_sig"] = self._sign(params)
        params["format"] = "json"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(LASTFM_API_URL, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        sk = data.get("session", {}).get("key")
                        if sk:
                            self._session_key = sk
                            return sk
        except Exception:
            logger.debug("lastfm_get_session_error", exc_info=True)
        return None

    # -- Scrobble + now-playing -----------------------------------------------

    async def scrobble(self, artist: str, track: str, album: str | None = None,
                       duration: int | None = None,
                       timestamp: int | None = None) -> bool:
        """Submit a scrobble to Last.fm.

        *timestamp* should be the UTC epoch second when the track started
        playing (per Last.fm spec).  Falls back to ``time.time()`` if omitted.
        """
        if not self._session_key:
            return False
        if not artist or not track:
            logger.warning("lastfm_scrobble_skip_empty", artist=artist, track=track)
            return False

        params = {
            "method": "track.scrobble",
            "api_key": self._api_key,
            "sk": self._session_key,
            "artist": artist,
            "track": track,
            "timestamp": str(timestamp or int(time.time())),
        }
        if album:
            params["album"] = album
        if duration and duration > 0:
            params["duration"] = str(duration // 1000)

        params["api_sig"] = self._sign(params)
        params["format"] = "json"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(LASTFM_API_URL, data=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("error"):
                            logger.warning("lastfm_scrobble_api_error",
                                           error=data.get("error"),
                                           message=data.get("message"),
                                           artist=artist, track=track)
                            return False
                        logger.info("lastfm_scrobbled", artist=artist, track=track)
                        return True
                    else:
                        body = await resp.text()
                        logger.warning("lastfm_scrobble_failed",
                                       status=resp.status, body=body[:200],
                                       artist=artist, track=track)
        except Exception:
            logger.warning("lastfm_scrobble_error", artist=artist, track=track, exc_info=True)
        return False

    async def update_now_playing(self, artist: str, track: str, album: str | None = None,
                                  duration: int | None = None) -> bool:
        """Update 'now playing' on Last.fm."""
        if not self._session_key:
            return False
        if not artist or not track:
            return False

        params = {
            "method": "track.updateNowPlaying",
            "api_key": self._api_key,
            "sk": self._session_key,
            "artist": artist,
            "track": track,
        }
        if album:
            params["album"] = album
        if duration and duration > 0:
            params["duration"] = str(duration // 1000)

        params["api_sig"] = self._sign(params)
        params["format"] = "json"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(LASTFM_API_URL, data=params) as resp:
                    if resp.status == 200:
                        logger.debug("lastfm_now_playing", artist=artist, track=track)
                        return True
                    else:
                        body = await resp.text()
                        logger.debug("lastfm_now_playing_failed",
                                     status=resp.status, body=body[:200])
        except Exception:
            logger.debug("lastfm_now_playing_error", exc_info=True)
        return False


def should_scrobble(duration_ms: int | None, listened_ms: int) -> bool:
    """Return True if the play qualifies for a scrobble per Last.fm rules.

    Rules (https://www.last.fm/api/scrobbling):
    - Track must be longer than 30 seconds.
    - Track must have been played for at least 50% of its duration
      OR for 4 minutes, whichever comes first.
    """
    if duration_ms and duration_ms < _SCROBBLE_MIN_DURATION_MS:
        return False
    if listened_ms <= 0:
        return False

    if duration_ms and duration_ms > 0:
        threshold = min(int(duration_ms * _SCROBBLE_PERCENT), _SCROBBLE_MAX_LISTEN_MS)
    else:
        # Unknown duration (e.g. radio) — require 4 minutes
        threshold = _SCROBBLE_MAX_LISTEN_MS

    return listened_ms >= threshold

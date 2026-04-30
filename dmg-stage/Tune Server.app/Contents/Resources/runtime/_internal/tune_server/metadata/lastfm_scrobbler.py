"""Last.fm scrobbling — submit played tracks to Last.fm."""

from __future__ import annotations

import hashlib
import time

import aiohttp
import structlog

logger = structlog.get_logger()

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"


class LastfmScrobbler:
    """Scrobble tracks to Last.fm using the Scrobble API (2.0)."""

    def __init__(self, api_key: str, api_secret: str, session_key: str | None = None) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._session_key = session_key

    @property
    def is_authenticated(self) -> bool:
        return bool(self._session_key)

    def _sign(self, params: dict) -> str:
        """Generate API signature for signed requests."""
        sig_str = "".join(f"{k}{params[k]}" for k in sorted(params)) + self._api_secret
        return hashlib.md5(sig_str.encode()).hexdigest()

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

    async def scrobble(self, artist: str, track: str, album: str | None = None,
                       duration: int | None = None) -> bool:
        """Submit a scrobble to Last.fm."""
        if not self._session_key:
            return False

        params = {
            "method": "track.scrobble",
            "api_key": self._api_key,
            "sk": self._session_key,
            "artist": artist,
            "track": track,
            "timestamp": str(int(time.time())),
        }
        if album:
            params["album"] = album
        if duration:
            params["duration"] = str(duration // 1000)

        params["api_sig"] = self._sign(params)
        params["format"] = "json"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(LASTFM_API_URL, data=params) as resp:
                    if resp.status == 200:
                        logger.info("lastfm_scrobbled", artist=artist, track=track)
                        return True
                    else:
                        body = await resp.text()
                        logger.warning("lastfm_scrobble_failed", status=resp.status, body=body[:200])
        except Exception:
            logger.debug("lastfm_scrobble_error", exc_info=True)
        return False

    async def update_now_playing(self, artist: str, track: str, album: str | None = None,
                                  duration: int | None = None) -> bool:
        """Update "now playing" on Last.fm."""
        if not self._session_key:
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
        if duration:
            params["duration"] = str(duration // 1000)

        params["api_sig"] = self._sign(params)
        params["format"] = "json"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(LASTFM_API_URL, data=params) as resp:
                    return resp.status == 200
        except Exception:
            logger.debug("lastfm_now_playing_error", exc_info=True)
        return False

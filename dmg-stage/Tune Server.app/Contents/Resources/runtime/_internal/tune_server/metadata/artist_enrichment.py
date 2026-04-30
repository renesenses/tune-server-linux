"""Client for mozaiklabs.fr artist metadata API."""

from __future__ import annotations

import time

import aiohttp
import structlog

from tune_server.config import settings

logger = structlog.get_logger()


class ArtistEnrichmentClient:
    """Client for mozaiklabs.fr artist metadata API."""

    def __init__(self, base_url: str | None = None):
        self._base_url = (base_url or settings.artist_metadata_url).rstrip("/")
        self._cache: dict[str, tuple[dict, float]] = {}  # mbid -> (data, timestamp)
        self._cache_ttl = 3600  # 1 hour local cache
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=settings.api_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _cache_get(self, mbid: str) -> dict | None:
        entry = self._cache.get(mbid)
        if entry is None:
            return None
        data, ts = entry
        if time.monotonic() - ts > self._cache_ttl:
            del self._cache[mbid]
            return None
        return data

    def _cache_set(self, mbid: str, data: dict) -> None:
        self._cache[mbid] = (data, time.monotonic())

    async def _request(self, path: str, params: dict | None = None) -> dict | list | None:
        """Make a GET request to the API. Returns parsed JSON or None on error."""
        url = f"{self._base_url}{path}"
        try:
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as exc:
            logger.warning("artist_enrichment_request_error", url=url, error=str(exc))
            return None
        except Exception:
            logger.exception("artist_enrichment_unexpected_error", url=url)
            return None

    async def get_artist(self, mbid: str) -> dict | None:
        """Get full artist metadata by MusicBrainz ID."""
        cached = self._cache_get(mbid)
        if cached is not None:
            return cached
        data = await self._request(f"/{mbid}")
        if data and isinstance(data, dict):
            # Fix relative image URLs from mozaiklabs.fr
            inner = data.get("data", data)
            img = inner.get("image_url", "")
            if img and img.startswith("/storage/"):
                base = self._base_url.rsplit("/api/", 1)[0]  # https://mozaiklabs.fr
                inner["image_url"] = f"{base}{img}"
            self._cache_set(mbid, data)
            return data
        return None

    async def get_bio(self, mbid: str, lang: str = "fr") -> str | None:
        """Get artist biography."""
        data = await self._request(f"/{mbid}/bio", params={"lang": lang})
        if data and isinstance(data, dict):
            return data.get("bio") or data.get("text")
        return None

    async def get_similar(self, mbid: str) -> list[dict]:
        """Get similar artists."""
        data = await self._request(f"/{mbid}/similar")
        if data and isinstance(data, list):
            return data
        if data and isinstance(data, dict):
            return data.get("artists", [])
        return []

    async def get_concerts(self, mbid: str) -> list[dict]:
        """Get upcoming concerts."""
        data = await self._request(f"/{mbid}/concerts")
        if data and isinstance(data, list):
            return data
        if data and isinstance(data, dict):
            return data.get("concerts", [])
        return []

    async def search(self, query: str) -> list[dict]:
        """Search artists by name."""
        data = await self._request("/search", params={"q": query})
        if data and isinstance(data, list):
            return data
        if data and isinstance(data, dict):
            return data.get("artists", [])
        return []

    async def refresh(self, mbid: str) -> dict | None:
        """Force re-enrichment by clearing cache and re-fetching."""
        self._cache.pop(mbid, None)
        url = f"{self._base_url}/{mbid}/refresh"
        try:
            session = await self._get_session()
            async with session.post(url) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                data = await resp.json()
                if data and isinstance(data, dict):
                    self._cache_set(mbid, data)
                    return data
                return None
        except aiohttp.ClientError as exc:
            logger.warning("artist_enrichment_refresh_error", mbid=mbid, error=str(exc))
            return None
        except Exception:
            logger.exception("artist_enrichment_refresh_unexpected", mbid=mbid)
            return None

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

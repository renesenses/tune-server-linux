from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class CachedUrl:
    url: str
    expires_at: float


class StreamUrlCache:
    """Time-limited cache for streaming service URLs."""

    def __init__(self, ttl_seconds: int = 300, max_size: int = 1000) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._cache: dict[str, CachedUrl] = {}

    def get(self, track_id: str) -> str | None:
        entry = self._cache.get(track_id)
        if entry and entry.expires_at > time.monotonic():
            return entry.url
        # Expired
        self._cache.pop(track_id, None)
        return None

    def set(self, track_id: str, url: str, ttl: int | None = None) -> None:
        if track_id not in self._cache and len(self._cache) >= self._max_size:
            # Remove oldest entry (first key in insertion-ordered dict)
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
        self._cache[track_id] = CachedUrl(
            url=url,
            expires_at=time.monotonic() + (ttl or self._ttl),
        )

    def invalidate(self, track_id: str) -> None:
        self._cache.pop(track_id, None)

    def clear(self) -> None:
        self._cache.clear()

    def cleanup(self) -> None:
        now = time.monotonic()
        expired = [k for k, v in self._cache.items() if v.expires_at <= now]
        for k in expired:
            del self._cache[k]

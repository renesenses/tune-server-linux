from __future__ import annotations

import time
from unittest.mock import patch

from tune_server.streaming.cache import StreamUrlCache


def test_set_and_get():
    cache = StreamUrlCache(ttl_seconds=300)
    cache.set("t1", "https://example.com/track1")
    assert cache.get("t1") == "https://example.com/track1"


def test_get_expired():
    cache = StreamUrlCache(ttl_seconds=300)
    # Set with a TTL that's already expired
    cache.set("t1", "https://example.com/track1", ttl=-1)
    assert cache.get("t1") is None


def test_get_missing():
    cache = StreamUrlCache(ttl_seconds=300)
    assert cache.get("nonexistent") is None


def test_custom_ttl():
    cache = StreamUrlCache(ttl_seconds=1)
    cache.set("t1", "https://example.com/track1", ttl=9999)
    # Should still be available because per-entry TTL is large
    assert cache.get("t1") == "https://example.com/track1"


def test_clear():
    cache = StreamUrlCache(ttl_seconds=300)
    cache.set("t1", "url1")
    cache.set("t2", "url2")
    cache.clear()
    assert cache.get("t1") is None
    assert cache.get("t2") is None


def test_cleanup_removes_expired():
    cache = StreamUrlCache(ttl_seconds=300)
    cache.set("valid", "url1", ttl=9999)
    cache.set("expired", "url2", ttl=-1)
    cache.cleanup()
    assert cache.get("valid") == "url1"
    assert cache.get("expired") is None


def test_get_removes_expired_entry():
    cache = StreamUrlCache(ttl_seconds=300)
    cache.set("t1", "url1", ttl=-1)
    assert cache.get("t1") is None
    # Internal cache should have removed the key
    assert "t1" not in cache._cache


def test_default_ttl():
    cache = StreamUrlCache(ttl_seconds=600)
    before = time.monotonic()
    cache.set("t1", "url1")
    entry = cache._cache["t1"]
    # expires_at should be approximately now + 600
    assert entry.expires_at >= before + 599
    assert entry.expires_at <= before + 601

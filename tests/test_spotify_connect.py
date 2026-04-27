from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tune_server.event_bus import EventBus
from tune_server.spotify_connect.daemon import (
    PCM_BITS_PER_SAMPLE,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    LibrespotDaemon,
)
from tune_server.spotify_connect.manager import (
    _PAUSE_EVENTS,
    _PLAY_EVENTS,
    _STOP_EVENTS,
    SpotifyConnectManager,
    _synthetic_track,
)
from tune_server.spotify_connect.relay import SpotifyConnectRelay, _wav_header


# ---------------------------------------------------------------------------
# WAV header
# ---------------------------------------------------------------------------

def test_wav_header_is_riff_44_bytes() -> None:
    h = _wav_header()
    assert len(h) == 44
    assert h[:4] == b"RIFF"
    assert h[8:12] == b"WAVE"
    assert h[12:16] == b"fmt "
    assert h[36:40] == b"data"


def test_wav_header_uses_unknown_size_for_streaming() -> None:
    h = _wav_header()
    # File size and data size both 0xFFFFFFFF (streaming, unknown EOF)
    assert h[4:8] == b"\xff\xff\xff\xff"
    assert h[40:44] == b"\xff\xff\xff\xff"


def test_pcm_constants_match_spotify_connect_protocol() -> None:
    # librespot emits S16LE 44.1kHz stereo by default; the relay assumes this.
    assert PCM_SAMPLE_RATE == 44100
    assert PCM_CHANNELS == 2
    assert PCM_BITS_PER_SAMPLE == 16


# ---------------------------------------------------------------------------
# Synthetic track
# ---------------------------------------------------------------------------

def test_synthetic_track_wraps_url() -> None:
    url = "http://10.0.0.1:8082/spotify-connect/stream.wav"
    t = _synthetic_track(url)
    assert t.file_path == url
    assert t.title == "Spotify Connect"
    assert t.sample_rate == 44100
    assert t.bit_depth == 16
    assert t.channels == 2


def test_event_sets_are_disjoint() -> None:
    # An event must trigger at most one action (play/pause/stop).
    assert _PLAY_EVENTS.isdisjoint(_PAUSE_EVENTS)
    assert _PLAY_EVENTS.isdisjoint(_STOP_EVENTS)
    assert _PAUSE_EVENTS.isdisjoint(_STOP_EVENTS)


# ---------------------------------------------------------------------------
# Manager state machine (with mocked daemon + zone)
# ---------------------------------------------------------------------------

class _FakeDaemon:
    """Stand-in for LibrespotDaemon. Exposes is_running and a controlled PCM stream."""

    def __init__(self) -> None:
        self.is_running = False
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self.is_running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.is_running = False

    async def read_pcm_chunk(self, n: int = 4096) -> bytes:
        # Simulate idle daemon (no audio yet)
        await asyncio.sleep(0.01)
        return b""


@pytest.fixture
def fake_zone():
    zone = MagicMock()
    zone.player.play = AsyncMock()
    zone.player.pause = AsyncMock()
    zone.player.stop = AsyncMock()
    return zone


@pytest.fixture
def fake_zone_manager(fake_zone):
    mgr = MagicMock()
    mgr.get_zone.return_value = fake_zone
    return mgr


@pytest.fixture
def manager(fake_zone_manager):
    bus = EventBus()
    m = SpotifyConnectManager(bus, zone_manager=fake_zone_manager)
    # Inject a fake daemon so we don't shell out to librespot in tests.
    m._daemon = _FakeDaemon()
    m._zone_id = 42
    return m


async def test_play_event_triggers_zone_play(manager, fake_zone) -> None:
    # Pre-condition: a relay so stream_url is non-None
    manager._relay = MagicMock()
    manager._relay.url_for.return_value = "http://test/stream.wav"
    await manager._handle_event(event="playing", track_id="abc", raw="")
    fake_zone.player.play.assert_awaited_once()
    assert manager._zone_playing is True


async def test_play_event_is_idempotent(manager, fake_zone) -> None:
    manager._relay = MagicMock()
    manager._relay.url_for.return_value = "http://test/stream.wav"
    await manager._handle_event(event="playing", track_id="abc", raw="")
    await manager._handle_event(event="playing", track_id="def", raw="")
    # The relay is a continuous stream — only ONE play() call expected.
    fake_zone.player.play.assert_awaited_once()


async def test_stop_event_stops_zone(manager, fake_zone) -> None:
    manager._zone_playing = True
    await manager._handle_event(event="stopped", track_id=None, raw="")
    fake_zone.player.stop.assert_awaited_once()
    assert manager._zone_playing is False


async def test_pause_event_pauses_zone(manager, fake_zone) -> None:
    manager._zone_playing = True
    await manager._handle_event(event="paused", track_id=None, raw="")
    fake_zone.player.pause.assert_awaited_once()


async def test_unknown_event_is_no_op(manager, fake_zone) -> None:
    await manager._handle_event(event="some_other_event", track_id=None, raw="")
    fake_zone.player.play.assert_not_awaited()
    fake_zone.player.stop.assert_not_awaited()
    fake_zone.player.pause.assert_not_awaited()


# ---------------------------------------------------------------------------
# Relay (HTTP)
# ---------------------------------------------------------------------------

async def test_relay_serves_wav_header_then_pcm() -> None:
    daemon = _FakeDaemon()
    daemon.is_running = True
    chunks_to_emit = [b"\x00\x01" * 1024, b"\x02\x03" * 1024, b""]

    async def _fake_read(n: int = 4096) -> bytes:
        if not chunks_to_emit:
            await asyncio.sleep(0.05)
            daemon.is_running = False
            return b""
        return chunks_to_emit.pop(0)

    daemon.read_pcm_chunk = _fake_read

    relay = SpotifyConnectRelay(daemon, host="127.0.0.1", port=18082)
    await relay.start()
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:18082/spotify-connect/stream.wav") as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"] == "audio/wav"
                # Read at least the WAV header
                body = await resp.content.readexactly(44)
                assert body[:4] == b"RIFF"
                assert body[8:12] == b"WAVE"
    finally:
        await relay.stop()

from __future__ import annotations

import asyncio

import pytest

from tune_server.models import AudioFormat, AudioStreamInfo
from tune_server.outputs.http_streamer import HttpAudioStreamer, StreamSession


@pytest.fixture
def stream_info():
    return AudioStreamInfo(
        format=AudioFormat.FLAC,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        duration_ms=240000,
    )


@pytest.fixture
def session(stream_info):
    return StreamSession("sess-1", stream_info)


@pytest.fixture
def streamer():
    return HttpAudioStreamer(host="0.0.0.0", port=9999)


# --- StreamSession ---


async def test_session_put_get(session):
    await session.put(b"chunk_data")
    result = await session.get()
    assert result == b"chunk_data"


async def test_session_close_sends_none(session):
    session.close()
    result = await session.get()
    assert result is None


async def test_session_close_deactivates(session):
    assert session.active is True
    session.close()
    assert session.active is False


def test_session_client_connected_event(session):
    assert not session.client_connected.is_set()
    session.client_connected.set()
    assert session.client_connected.is_set()


# --- HttpAudioStreamer (session management) ---


def test_create_session(streamer, stream_info):
    stream_id = streamer.create_session(stream_info)
    assert stream_id is not None
    assert len(stream_id) > 0
    assert streamer.get_session(stream_id) is not None


def test_get_session(streamer, stream_info):
    stream_id = streamer.create_session(stream_info)
    session = streamer.get_session(stream_id)
    assert session is not None
    assert session.stream_id == stream_id


def test_get_session_missing(streamer):
    assert streamer.get_session("nonexistent") is None


def test_remove_session(streamer, stream_info):
    stream_id = streamer.create_session(stream_info)
    streamer.remove_session(stream_id)
    assert streamer.get_session(stream_id) is None


def test_get_stream_url(streamer, stream_info):
    stream_id = streamer.create_session(stream_info)
    url = streamer.get_stream_url(stream_id, "192.168.1.100")
    assert "192.168.1.100" in url
    assert "9999" in url
    assert stream_id in url
    assert url.endswith(".flac")


async def test_stop_closes_all_sessions(streamer, stream_info):
    s1 = streamer.create_session(stream_info)
    s2 = streamer.create_session(stream_info)

    await streamer.stop()

    assert streamer.get_session(s1) is None
    assert streamer.get_session(s2) is None

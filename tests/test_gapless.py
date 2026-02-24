from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tune_server.audio.formats import LOCAL_CAPABILITIES
from tune_server.models import AudioFormat, AudioStreamInfo, Track
from tune_server.playback.gapless import GaplessHandler


@pytest.fixture
def handler():
    return GaplessHandler(LOCAL_CAPABILITIES)


@pytest.fixture
def sample_track():
    return Track(
        id=1,
        title="Test Track",
        file_path="/music/test.flac",
        format=AudioFormat.FLAC,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        duration_ms=240000,
    )


@pytest.fixture
def mock_stream_info():
    return AudioStreamInfo(
        format=AudioFormat.FLAC,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        duration_ms=240000,
    )


def test_initial_state(handler):
    assert handler.has_next is False
    assert handler.next_track is None
    assert handler.next_stream_info is None


@patch("tune_server.playback.gapless.AudioPipeline")
async def test_preload_sets_next_track(mock_pipeline_cls, handler, sample_track, mock_stream_info):
    mock_pipeline = AsyncMock()
    mock_pipeline.start = AsyncMock(return_value=mock_stream_info)
    mock_pipeline_cls.return_value = mock_pipeline

    await handler.preload(sample_track)
    # Wait for the background task to finish
    await handler._preload_task

    assert handler.next_track is sample_track


@patch("tune_server.playback.gapless.AudioPipeline")
async def test_preload_creates_pipeline(mock_pipeline_cls, handler, sample_track, mock_stream_info):
    mock_pipeline = AsyncMock()
    mock_pipeline.start = AsyncMock(return_value=mock_stream_info)
    mock_pipeline_cls.return_value = mock_pipeline

    await handler.preload(sample_track)
    await handler._preload_task

    mock_pipeline.start.assert_awaited_once()


@patch("tune_server.playback.gapless.AudioPipeline")
async def test_has_next_after_preload(mock_pipeline_cls, handler, sample_track, mock_stream_info):
    mock_pipeline = AsyncMock()
    mock_pipeline.start = AsyncMock(return_value=mock_stream_info)
    mock_pipeline_cls.return_value = mock_pipeline

    await handler.preload(sample_track)
    await handler._preload_task

    assert handler.has_next is True


@patch("tune_server.playback.gapless.AudioPipeline")
async def test_take_pipeline(mock_pipeline_cls, handler, sample_track, mock_stream_info):
    mock_pipeline = AsyncMock()
    mock_pipeline.start = AsyncMock(return_value=mock_stream_info)
    mock_pipeline_cls.return_value = mock_pipeline

    await handler.preload(sample_track)
    await handler._preload_task

    pipeline, info = handler.take_pipeline()
    assert pipeline is mock_pipeline
    assert info is mock_stream_info


@patch("tune_server.playback.gapless.AudioPipeline")
async def test_take_pipeline_clears_state(mock_pipeline_cls, handler, sample_track, mock_stream_info):
    mock_pipeline = AsyncMock()
    mock_pipeline.start = AsyncMock(return_value=mock_stream_info)
    mock_pipeline_cls.return_value = mock_pipeline

    await handler.preload(sample_track)
    await handler._preload_task

    handler.take_pipeline()
    assert handler.has_next is False
    assert handler.next_track is None


@patch("tune_server.playback.gapless.AudioPipeline")
async def test_cancel_stops_pipeline(mock_pipeline_cls, handler, sample_track, mock_stream_info):
    mock_pipeline = AsyncMock()
    mock_pipeline.start = AsyncMock(return_value=mock_stream_info)
    mock_pipeline_cls.return_value = mock_pipeline

    await handler.preload(sample_track)
    await handler._preload_task

    await handler.cancel()
    mock_pipeline.stop.assert_awaited()
    assert handler.has_next is False


@patch("tune_server.playback.gapless.AudioPipeline")
async def test_preload_cancels_previous(mock_pipeline_cls, handler, sample_track, mock_stream_info):
    mock_pipeline1 = AsyncMock()
    mock_pipeline1.start = AsyncMock(return_value=mock_stream_info)

    mock_pipeline2 = AsyncMock()
    mock_pipeline2.start = AsyncMock(return_value=mock_stream_info)

    mock_pipeline_cls.side_effect = [mock_pipeline1, mock_pipeline2]

    track2 = Track(
        id=2, title="Track 2", file_path="/music/test2.flac",
        format=AudioFormat.FLAC, sample_rate=44100, bit_depth=16, channels=2,
    )

    await handler.preload(sample_track)
    await handler._preload_task

    await handler.preload(track2)
    await handler._preload_task

    # First pipeline should have been stopped during cancel
    mock_pipeline1.stop.assert_awaited()
    assert handler.next_track is track2

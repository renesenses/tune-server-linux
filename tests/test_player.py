from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from tune_server.audio.buffer import AsyncRingBuffer
from tune_server.audio.formats import AudioCapabilities
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import AudioFormat, AudioStreamInfo, PlaybackState, Track
from tune_server.playback.player import Player


def _make_track(track_id: int = 1, title: str = "Test Track") -> Track:
    return Track(
        id=track_id,
        title=title,
        file_path=f"/music/{track_id}.flac",
        format=AudioFormat.FLAC,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )


def _mock_pipeline(close_immediately: bool = True):
    """Create a mock AudioPipeline.

    If close_immediately=True, the buffer is pre-closed so the playback loop
    finishes right away (good for stop/cleanup tests).
    If False, the buffer stays open so the player remains in PLAYING state.
    """
    pipeline = AsyncMock()
    buf = AsyncRingBuffer(max_chunks=4)
    if close_immediately:
        buf.close()
    pipeline.output_buffer = buf
    pipeline.start = AsyncMock(
        return_value=AudioStreamInfo(
            format=AudioFormat.FLAC,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
    )
    pipeline.stop = AsyncMock()
    return pipeline


class TestInitialState:
    async def test_initial_state_stopped(self):
        player = Player(zone_id=1, event_bus=EventBus())
        assert player.state == PlaybackState.STOPPED
        assert player.current_track is None
        assert player.position_ms == 0
        assert player.volume == 0.5


class TestPlay:
    @patch("tune_server.playback.player.AudioPipeline")
    async def test_play_starts_track(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        tracks = [_make_track(1, "Song A"), _make_track(2, "Song B")]
        await player.play(tracks=tracks)

        await asyncio.sleep(0.05)

        mock_output.start.assert_awaited()
        assert player.current_track is not None
        assert player.state == PlaybackState.PLAYING

        # Clean up
        pipeline.output_buffer.close()
        await player.stop()

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_play_emits_event(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        captured = []

        async def capture(event: Event):
            captured.append(event)

        event_bus.on(EventType.PLAYBACK_STARTED, capture)

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.play(tracks=[_make_track(1)])

        await asyncio.sleep(0.05)

        started_events = [e for e in captured if e.type == EventType.PLAYBACK_STARTED]
        assert len(started_events) >= 1
        assert started_events[0].data["zone_id"] == 1
        assert started_events[0].data["track_id"] == 1

        pipeline.output_buffer.close()
        await player.stop()

    async def test_play_no_tracks_noop(self, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        await player.play(tracks=[])
        assert player.state == PlaybackState.STOPPED

    async def test_play_no_output_logs_error(self, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        player.queue.set_tracks([_make_track()])
        # No output set — _start_track should log error but not crash
        await player._start_track(_make_track())
        # The important thing is: no crash


class TestOutputStartError:
    @patch("tune_server.playback.player.AudioPipeline")
    async def test_output_error_stops_gracefully(self, MockPipeline, event_bus):
        """If output.start() raises, player should stop instead of advancing."""
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        failing_output = AsyncMock()
        type(failing_output).capabilities = PropertyMock(
            return_value=AudioCapabilities(
                formats={AudioFormat.FLAC}, max_sample_rate=44100, max_bit_depth=16,
            )
        )
        failing_output.start = AsyncMock(
            side_effect=RuntimeError("AirPlay auth failed")
        )

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(failing_output)

        tracks = [_make_track(1, "Track 1"), _make_track(2, "Track 2")]
        await player.play(tracks=tracks)
        await asyncio.sleep(0.05)

        # Should be stopped, NOT have advanced to track 2
        assert player.state == PlaybackState.STOPPED

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_output_error_does_not_advance_queue(self, MockPipeline, event_bus):
        """Queue should stay at position 0 when output fails."""
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        failing_output = AsyncMock()
        type(failing_output).capabilities = PropertyMock(
            return_value=AudioCapabilities(
                formats={AudioFormat.FLAC}, max_sample_rate=44100, max_bit_depth=16,
            )
        )
        failing_output.start = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(failing_output)

        tracks = [_make_track(1), _make_track(2), _make_track(3)]
        await player.play(tracks=tracks)
        await asyncio.sleep(0.05)

        assert player.queue.position == 0
        assert failing_output.start.await_count == 1


class TestPauseResume:
    @patch("tune_server.playback.player.AudioPipeline")
    async def test_pause_from_playing(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.play(tracks=[_make_track()])
        await asyncio.sleep(0.05)

        assert player.state == PlaybackState.PLAYING
        await player.pause()

        assert player.state == PlaybackState.PAUSED
        mock_output.pause.assert_awaited()

        pipeline.output_buffer.close()
        await player.stop()

    async def test_pause_from_stopped_noop(self, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        await player.pause()
        assert player.state == PlaybackState.STOPPED

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_resume_from_paused(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.play(tracks=[_make_track()])
        await asyncio.sleep(0.05)

        await player.pause()
        await player.resume()

        assert player.state == PlaybackState.PLAYING
        mock_output.resume.assert_awaited()

        pipeline.output_buffer.close()
        await player.stop()


class TestStop:
    @patch("tune_server.playback.player.AudioPipeline")
    async def test_stop(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.play(tracks=[_make_track()])
        await asyncio.sleep(0.05)

        await player.stop()

        assert player.state == PlaybackState.STOPPED
        assert player.position_ms == 0
        mock_output.stop.assert_awaited()


class TestVolume:
    async def test_set_volume(self, mock_output, event_bus):
        captured = []

        async def capture(event: Event):
            captured.append(event)

        event_bus.on(EventType.ZONE_VOLUME_CHANGED, capture)

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        await player.set_volume(0.8)
        assert player.volume == 0.8
        mock_output.set_volume.assert_awaited_with(0.8)

        # Check event emitted
        assert len(captured) == 1
        assert captured[0].data["volume"] == 0.8

    async def test_set_volume_clamps(self, mock_output, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        await player.set_volume(1.5)
        assert player.volume == 1.0

        await player.set_volume(-0.5)
        assert player.volume == 0.0


class TestSkip:
    @patch("tune_server.playback.player.AudioPipeline")
    async def test_skip_next(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        tracks = [_make_track(1, "A"), _make_track(2, "B"), _make_track(3, "C")]
        player.queue.set_tracks(tracks)

        await player.skip_next()
        await asyncio.sleep(0.05)

        assert player.queue.position == 1

        pipeline.output_buffer.close()
        await player.stop()

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_skip_next_empty_stops(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        player.queue.set_tracks([_make_track()])
        # next() returns None when at end
        await player.skip_next()
        await asyncio.sleep(0.05)

        assert player.state == PlaybackState.STOPPED

        pipeline.output_buffer.close()

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_skip_previous(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        tracks = [_make_track(1, "A"), _make_track(2, "B")]
        player.queue.set_tracks(tracks)
        player.queue.next()  # advance to track 2

        await player.skip_previous()
        await asyncio.sleep(0.05)

        assert player.queue.position == 0

        pipeline.output_buffer.close()
        await player.stop()


class TestCleanup:
    @patch("tune_server.playback.player.AudioPipeline")
    async def test_cleanup(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.play(tracks=[_make_track()])
        await asyncio.sleep(0.05)

        await player.cleanup()

        assert player.state == PlaybackState.STOPPED
        mock_output.close.assert_awaited()

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


# =========================================================================
# Additional tests — state machine, gapless, seek, volume, error handling
# =========================================================================


class TestStateMachineTransitions:
    """Verify the full state machine: STOPPED → PLAYING → PAUSED → STOPPED
    and that invalid/no-op transitions are handled gracefully."""

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_full_lifecycle_stopped_playing_paused_resumed_stopped(
        self, MockPipeline, mock_output, event_bus
    ):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        # STOPPED
        assert player.state == PlaybackState.STOPPED

        # STOPPED → PLAYING
        await player.play(tracks=[_make_track()])
        await asyncio.sleep(0.05)
        assert player.state == PlaybackState.PLAYING

        # PLAYING → PAUSED
        await player.pause()
        assert player.state == PlaybackState.PAUSED

        # PAUSED → PLAYING (resume)
        await player.resume()
        assert player.state == PlaybackState.PLAYING

        # PLAYING → STOPPED
        pipeline.output_buffer.close()
        await player.stop()
        assert player.state == PlaybackState.STOPPED

    async def test_resume_from_stopped_is_noop(self, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        await player.resume()
        assert player.state == PlaybackState.STOPPED

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_double_pause_is_noop(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.play(tracks=[_make_track()])
        await asyncio.sleep(0.05)

        await player.pause()
        assert player.state == PlaybackState.PAUSED
        # Second pause should be a no-op (already paused, not PLAYING)
        await player.pause()
        assert player.state == PlaybackState.PAUSED
        # Output.pause should have been called exactly once
        assert mock_output.pause.await_count == 1

        pipeline.output_buffer.close()
        await player.stop()

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_double_resume_is_noop(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.play(tracks=[_make_track()])
        await asyncio.sleep(0.05)

        await player.pause()
        await player.resume()
        assert player.state == PlaybackState.PLAYING
        # Second resume: already PLAYING, should be a no-op
        await player.resume()
        assert player.state == PlaybackState.PLAYING
        assert mock_output.resume.await_count == 1

        pipeline.output_buffer.close()
        await player.stop()

    async def test_stop_from_stopped_is_safe(self, mock_output, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        # Calling stop when already stopped should not crash
        await player.stop()
        assert player.state == PlaybackState.STOPPED

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_play_while_playing_restarts(self, MockPipeline, mock_output, event_bus):
        """Calling play() again while playing should stop the old track and
        start the new one."""
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        await player.play(tracks=[_make_track(1, "First")])
        await asyncio.sleep(0.05)
        assert player.state == PlaybackState.PLAYING

        # Play a different set of tracks
        pipeline2 = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline2
        await player.play(tracks=[_make_track(2, "Second")])
        await asyncio.sleep(0.05)

        assert player.state == PlaybackState.PLAYING
        assert player.current_track.title == "Second"

        pipeline2.output_buffer.close()
        await player.stop()


class TestGaplessPreBuffering:
    """Gapless handler preloads next track so transitions are seamless."""

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_preload_called_after_play(self, MockPipeline, mock_output, event_bus):
        """When starting playback with multiple tracks, _preload_next should
        prepare the next track via the GaplessHandler."""
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        tracks = [_make_track(1, "A"), _make_track(2, "B")]
        with patch.object(player, "_preload_next", new_callable=AsyncMock) as mock_preload:
            await player.play(tracks=tracks)
            await asyncio.sleep(0.05)
            mock_preload.assert_awaited()

        pipeline.output_buffer.close()
        await player.stop()

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_no_preload_when_single_track(self, MockPipeline, mock_output, event_bus):
        """With a single track queue, peek_next returns None so preload is
        effectively a no-op (no crash, no unnecessary pipeline start)."""
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        tracks = [_make_track(1, "Only")]
        await player.play(tracks=tracks)
        await asyncio.sleep(0.05)

        # peek_next should be None
        assert player.queue.peek_next() is None

        pipeline.output_buffer.close()
        await player.stop()

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_preload_not_called_when_gapless_unsupported(
        self, MockPipeline, event_bus
    ):
        """If output capabilities don't support gapless, _preload_next should
        short-circuit without calling gapless.preload."""
        from tune_server.audio.formats import AIRPLAY_CAPABILITIES

        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        output = AsyncMock()
        output.name = "airplay-mock"
        type(output).capabilities = PropertyMock(return_value=AIRPLAY_CAPABILITIES)
        type(output).is_available = PropertyMock(return_value=True)

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(output)

        tracks = [_make_track(1, "A"), _make_track(2, "B")]
        await player.play(tracks=tracks)
        await asyncio.sleep(0.05)

        # AirPlay now supports gapless (pre-transcode next track)
        assert output.capabilities.supports_gapless

        pipeline.output_buffer.close()
        await player.stop()


class TestPauseResumeOutputInteraction:
    """Verify output.pause/resume are forwarded and events are emitted."""

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_pause_emits_event_with_position(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        captured = []

        async def capture(event: Event):
            captured.append(event)

        event_bus.on(EventType.PLAYBACK_PAUSED, capture)

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.play(tracks=[_make_track()])
        await asyncio.sleep(0.05)

        await player.pause()

        paused = [e for e in captured if e.type == EventType.PLAYBACK_PAUSED]
        assert len(paused) == 1
        assert paused[0].data["zone_id"] == 1
        assert "position_ms" in paused[0].data

        pipeline.output_buffer.close()
        await player.stop()

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_resume_emits_event(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        captured = []

        async def capture(event: Event):
            captured.append(event)

        event_bus.on(EventType.PLAYBACK_RESUMED, capture)

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.play(tracks=[_make_track()])
        await asyncio.sleep(0.05)

        await player.pause()
        await player.resume()

        resumed = [e for e in captured if e.type == EventType.PLAYBACK_RESUMED]
        assert len(resumed) == 1
        assert resumed[0].data["zone_id"] == 1

        pipeline.output_buffer.close()
        await player.stop()

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_pause_freezes_position(self, MockPipeline, mock_output, event_bus):
        """After pausing, position_ms should stop advancing."""
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.play(tracks=[_make_track()])
        await asyncio.sleep(0.05)

        await player.pause()
        pos_after_pause = player.position_ms
        await asyncio.sleep(0.05)
        pos_later = player.position_ms
        # Position should NOT advance while paused
        assert pos_later == pos_after_pause

        pipeline.output_buffer.close()
        await player.stop()


class TestSeekOperations:
    """Seek to a position within the current track."""

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_seek_native_success(self, MockPipeline, mock_output, event_bus):
        """If native output seek succeeds, position updates without pipeline restart."""
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        mock_output.seek = AsyncMock(return_value=True)

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        track = _make_track(1, "Song")
        track.duration_ms = 300_000
        await player.play(tracks=[track])
        await asyncio.sleep(0.05)

        await player.seek(120_000)

        # Position should be approximately 120_000
        assert abs(player.position_ms - 120_000) < 500
        mock_output.seek.assert_awaited_with(120_000)

        pipeline.output_buffer.close()
        await player.stop()

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_seek_negative_clamped_to_zero(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline
        mock_output.seek = AsyncMock(return_value=True)

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        track = _make_track(1, "Song")
        track.duration_ms = 300_000
        await player.play(tracks=[track])
        await asyncio.sleep(0.05)

        await player.seek(-5000)
        # Should have been clamped to 0
        mock_output.seek.assert_awaited_with(0)

        pipeline.output_buffer.close()
        await player.stop()

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_seek_past_duration_clamped(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline
        mock_output.seek = AsyncMock(return_value=True)

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        track = _make_track(1, "Song")
        track.duration_ms = 200_000
        await player.play(tracks=[track])
        await asyncio.sleep(0.05)

        await player.seek(999_999)
        # Should clamp to duration_ms
        mock_output.seek.assert_awaited_with(200_000)

        pipeline.output_buffer.close()
        await player.stop()

    async def test_seek_no_current_track_noop(self, event_bus):
        """Seeking with no track loaded should be a no-op."""
        player = Player(zone_id=1, event_bus=event_bus)
        await player.seek(50_000)
        assert player.state == PlaybackState.STOPPED

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_seek_fallback_pipeline_restart(self, MockPipeline, mock_output, event_bus):
        """When native seek returns False, player falls back to pipeline restart."""
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline
        mock_output.seek = AsyncMock(return_value=False)

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        track = _make_track(1, "Song")
        track.duration_ms = 300_000
        await player.play(tracks=[track])
        await asyncio.sleep(0.05)

        # Create a fresh pipeline for the restart
        pipeline2 = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline2

        await player.seek(60_000)
        await asyncio.sleep(0.05)

        # Pipeline was restarted via _start_track with seek_ms
        assert player.state == PlaybackState.PLAYING

        pipeline2.output_buffer.close()
        await player.stop()


class TestVolumeControl:
    """Additional volume control edge cases."""

    async def test_set_volume_without_output(self, event_bus):
        """Setting volume with no output assigned should still update internal state."""
        player = Player(zone_id=1, event_bus=event_bus)
        await player.set_volume(0.75)
        assert player.volume == 0.75

    async def test_set_volume_callback_invoked(self, mock_output, event_bus):
        """Volume change callback should be called when volume changes."""
        cb = AsyncMock()
        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        player.set_volume_change_callback(cb)

        await player.set_volume(0.6)

        cb.assert_awaited_once_with(0.6)

    async def test_volume_clamp_zero(self, mock_output, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.set_volume(0.0)
        assert player.volume == 0.0
        mock_output.set_volume.assert_awaited_with(0.0)

    async def test_volume_clamp_one(self, mock_output, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.set_volume(1.0)
        assert player.volume == 1.0
        mock_output.set_volume.assert_awaited_with(1.0)

    async def test_volume_preserves_across_states(self, mock_output, event_bus):
        """Volume should persist even after stop."""
        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.set_volume(0.3)
        await player.stop()
        assert player.volume == 0.3


class TestErrorHandling:
    """Error scenarios: output failure during play, decode failure, etc."""

    @patch("tune_server.playback.player.create_pipeline")
    async def test_pipeline_start_error_emits_event(self, mock_create, mock_output, event_bus):
        """If AudioPipeline.start() raises, player emits a PLAYBACK_ERROR event
        and transitions to STOPPED."""
        pipeline = AsyncMock()
        pipeline.start = AsyncMock(side_effect=RuntimeError("decode error"))
        pipeline.stop = AsyncMock()
        mock_create.return_value = pipeline

        # Ensure output does NOT claim direct URL support
        mock_output.supports_direct_url = MagicMock(return_value=False)
        type(mock_output).is_direct_url = PropertyMock(return_value=False)

        captured = []

        async def capture(event: Event):
            captured.append(event)

        event_bus.on(EventType.PLAYBACK_ERROR, capture)

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)

        await player.play(tracks=[_make_track(1, "Bad File")])
        await asyncio.sleep(0.05)

        assert player.state == PlaybackState.STOPPED
        error_events = [e for e in captured if e.type == EventType.PLAYBACK_ERROR]
        assert len(error_events) >= 1
        assert "pipeline_error" in error_events[0].data["error"]

    @patch("tune_server.playback.player.create_pipeline")
    async def test_pipeline_start_timeout_stops(self, mock_create, mock_output, event_bus):
        """If pipeline.start() times out, player should stop gracefully."""
        pipeline = AsyncMock()

        async def slow_start(*args, **kwargs):
            await asyncio.sleep(999)

        pipeline.start = slow_start
        pipeline.stop = AsyncMock()
        mock_create.return_value = pipeline

        # Ensure output does NOT claim direct URL support
        mock_output.supports_direct_url = MagicMock(return_value=False)
        type(mock_output).is_direct_url = PropertyMock(return_value=False)

        # Patch the timeout to be very short
        with patch("tune_server.playback.player.settings") as mock_settings:
            mock_settings.pipeline_start_timeout = 0.01
            mock_settings.stream_url_resolve_timeout = 10

            player = Player(zone_id=1, event_bus=event_bus)
            player.set_output(mock_output)

            await player.play(tracks=[_make_track(1, "Slow")])
            await asyncio.sleep(0.1)

            assert player.state == PlaybackState.STOPPED

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_output_start_error_emits_playback_error(
        self, MockPipeline, event_bus
    ):
        """When output.start() fails, a PLAYBACK_ERROR event should be emitted."""
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        failing_output = AsyncMock()
        type(failing_output).capabilities = PropertyMock(
            return_value=AudioCapabilities(
                formats={AudioFormat.FLAC}, max_sample_rate=44100, max_bit_depth=16,
            )
        )
        failing_output.start = AsyncMock(
            side_effect=ConnectionError("renderer offline")
        )

        captured = []

        async def capture(event: Event):
            captured.append(event)

        event_bus.on(EventType.PLAYBACK_ERROR, capture)

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(failing_output)

        await player.play(tracks=[_make_track(1, "Track")])
        await asyncio.sleep(0.05)

        assert player.state == PlaybackState.STOPPED
        error_events = [e for e in captured if e.type == EventType.PLAYBACK_ERROR]
        assert len(error_events) >= 1
        assert error_events[0].data["error"] in ("output_error", "pipeline_error")

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_output_unavailable_emits_error(self, MockPipeline, event_bus):
        """If output._available is False, starting a track should emit an error
        and not crash."""
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        output = AsyncMock()
        type(output).capabilities = PropertyMock(
            return_value=AudioCapabilities(
                formats={AudioFormat.FLAC}, max_sample_rate=44100, max_bit_depth=16,
            )
        )
        output._available = False

        captured = []

        async def capture(event: Event):
            captured.append(event)

        event_bus.on(EventType.PLAYBACK_ERROR, capture)

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(output)

        track = _make_track(1, "Track")
        await player._start_track(track)
        await asyncio.sleep(0.05)

        error_events = [e for e in captured if e.type == EventType.PLAYBACK_ERROR]
        assert len(error_events) == 1
        assert error_events[0].data["error"] == "output_unavailable"


class TestPlayerNoOutput:
    """Player behavior when no output has been assigned."""

    async def test_start_track_no_output_no_crash(self, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        # No set_output called
        await player._start_track(_make_track())
        # Should have returned early — no crash, state still STOPPED
        assert player.state == PlaybackState.STOPPED

    async def test_play_no_output_no_crash(self, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        await player.play(tracks=[_make_track()])
        # Should not crash — just logs error
        assert player.state == PlaybackState.STOPPED

    async def test_set_volume_no_output(self, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        await player.set_volume(0.9)
        assert player.volume == 0.9

    async def test_pause_no_output_noop(self, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        await player.pause()
        assert player.state == PlaybackState.STOPPED

    async def test_stop_no_output_safe(self, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        await player.stop()
        assert player.state == PlaybackState.STOPPED

    async def test_seek_no_output_noop(self, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        player.queue.set_tracks([_make_track()])
        await player.seek(10_000)
        assert player.state == PlaybackState.STOPPED

    async def test_cleanup_no_output(self, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        await player.cleanup()
        assert player.state == PlaybackState.STOPPED


class TestStopEmitsEvent:
    """Stop should always emit PLAYBACK_STOPPED event."""

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_stop_emits_stopped_event(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        captured = []

        async def capture(event: Event):
            captured.append(event)

        event_bus.on(EventType.PLAYBACK_STOPPED, capture)

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.play(tracks=[_make_track()])
        await asyncio.sleep(0.05)

        pipeline.output_buffer.close()
        await player.stop()

        stopped = [e for e in captured if e.type == EventType.PLAYBACK_STOPPED]
        assert len(stopped) >= 1
        assert stopped[0].data["zone_id"] == 1

    async def test_stop_resets_position(self, mock_output, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        player._position_ms = 42_000
        await player.stop()
        assert player.position_ms == 0

    async def test_stop_clears_signal_path(self, mock_output, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        player._signal_path = MagicMock()
        await player.stop()
        assert player.signal_path is None


class TestPositionTracking:
    """Position should advance during PLAYING and freeze during PAUSED/STOPPED."""

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_position_advances_while_playing(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        await player.play(tracks=[_make_track()])
        await asyncio.sleep(0.05)

        pos1 = player.position_ms
        await asyncio.sleep(0.05)
        pos2 = player.position_ms
        assert pos2 > pos1

        pipeline.output_buffer.close()
        await player.stop()

    async def test_position_zero_when_stopped(self, event_bus):
        player = Player(zone_id=1, event_bus=event_bus)
        assert player.position_ms == 0


class TestSkipInProgressGuard:
    """The _skip_in_progress flag prevents concurrent skip operations."""

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_skip_next_blocked_during_skip(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        tracks = [_make_track(1, "A"), _make_track(2, "B"), _make_track(3, "C")]
        player.queue.set_tracks(tracks)

        # Simulate skip in progress
        player._skip_in_progress = True
        await player.skip_next()
        # Queue position should NOT have changed
        assert player.queue.position == 0

        player._skip_in_progress = False
        pipeline.output_buffer.close()

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_seek_blocked_during_skip(self, MockPipeline, mock_output, event_bus):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(mock_output)
        tracks = [_make_track(1, "A")]
        tracks[0].duration_ms = 300_000
        await player.play(tracks=tracks)
        await asyncio.sleep(0.05)

        player._skip_in_progress = True
        mock_output.seek = AsyncMock(return_value=True)
        await player.seek(100_000)
        # Seek should have been skipped — output.seek not called
        mock_output.seek.assert_not_awaited()

        player._skip_in_progress = False
        pipeline.output_buffer.close()
        await player.stop()

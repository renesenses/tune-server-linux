"""Tests for streaming playback: pipeline URL support, stream URL resolution, browse endpoints, auth persistence."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from tune_server.audio.buffer import AsyncRingBuffer
from tune_server.audio.formats import AudioCapabilities
from tune_server.audio.pipeline import AudioPipeline
from tune_server.db.engine import Database
from tune_server.event_bus import EventBus
from tune_server.models import (
    Album,
    Artist,
    AudioFormat,
    AudioStreamInfo,
    PlaybackState,
    Source,
    Track,
)
from tune_server.playback.player import Player


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _streaming_track(source: Source = Source.TIDAL, source_id: str = "12345") -> Track:
    return Track(
        title="Streaming Song",
        artist_name="Artist",
        duration_ms=200_000,
        format=AudioFormat.FLAC,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        source=source,
        source_id=source_id,
        # file_path intentionally None
    )


def _local_track() -> Track:
    return Track(
        id=1,
        title="Local Song",
        file_path="/music/song.flac",
        format=AudioFormat.FLAC,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )


def _mock_pipeline(close_immediately: bool = True):
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


# ---------------------------------------------------------------------------
# 1. Pipeline URL support
# ---------------------------------------------------------------------------

class TestPipelineUrlDetection:
    """AudioPipeline.start() should force decode path for HTTP URLs."""

    async def test_url_forces_decode_path(self):
        """When given an HTTP URL, passthrough should be skipped even if format matches."""
        caps = AudioCapabilities(
            formats={AudioFormat.FLAC},
            max_sample_rate=192000,
            max_bit_depth=24,
        )
        pipeline = AudioPipeline(caps)

        # Patch FFmpegDecoder so we don't actually spawn ffmpeg
        with patch("tune_server.audio.pipeline.FFmpegDecoder") as MockDecoder:
            mock_dec = AsyncMock()
            mock_buf = AsyncRingBuffer(max_chunks=4)
            mock_buf.close()
            mock_dec.buffer = mock_buf
            mock_dec.start = AsyncMock()
            MockDecoder.return_value = mock_dec

            stream_info = await pipeline.start(
                file_path="https://stream.example.com/track.flac",
                source_format=AudioFormat.FLAC,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            )

            # Should NOT be passthrough (even though format matches capabilities)
            assert not pipeline.is_passthrough
            # Decoder should have been started
            MockDecoder.assert_called_once()
            mock_dec.start.assert_awaited_once()

        await pipeline.stop()

    async def test_local_file_still_uses_passthrough(self):
        """Local files should still use passthrough when format matches."""
        caps = AudioCapabilities(
            formats={AudioFormat.FLAC},
            max_sample_rate=192000,
            max_bit_depth=24,
        )
        pipeline = AudioPipeline(caps)

        with patch("tune_server.audio.pipeline.Path") as MockPath:
            mock_path_inst = MagicMock()
            mock_path_inst.exists.return_value = True
            mock_path_inst.stat.return_value = MagicMock(st_size=1000)
            MockPath.return_value = mock_path_inst

            stream_info = await pipeline.start(
                file_path="/music/song.flac",
                source_format=AudioFormat.FLAC,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            )

            assert pipeline.is_passthrough

        await pipeline.stop()


# ---------------------------------------------------------------------------
# 2. Stream URL resolver on Player
# ---------------------------------------------------------------------------

class TestStreamUrlResolver:
    """Player should call stream_url_resolver before starting streaming tracks."""

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_resolver_called_for_streaming_track(self, MockPipeline):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        event_bus = EventBus()
        output = AsyncMock()
        type(output).capabilities = PropertyMock(
            return_value=AudioCapabilities(
                formats={AudioFormat.FLAC}, max_sample_rate=44100, max_bit_depth=16,
            )
        )

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(output)

        resolver = AsyncMock(return_value="https://stream.example.com/track.flac")
        player.set_stream_url_resolver(resolver)

        track = _streaming_track()
        await player.play(tracks=[track])
        await asyncio.sleep(0.05)

        resolver.assert_awaited_once()
        # Track should now have file_path set
        assert track.file_path == "https://stream.example.com/track.flac"

        pipeline.output_buffer.close()
        await player.stop()

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_resolver_not_called_for_local_track(self, MockPipeline):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        event_bus = EventBus()
        output = AsyncMock()
        type(output).capabilities = PropertyMock(
            return_value=AudioCapabilities(
                formats={AudioFormat.FLAC}, max_sample_rate=44100, max_bit_depth=16,
            )
        )

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(output)

        resolver = AsyncMock(return_value="https://example.com/x.flac")
        player.set_stream_url_resolver(resolver)

        track = _local_track()
        await player.play(tracks=[track])
        await asyncio.sleep(0.05)

        # Resolver should NOT be called for local tracks (file_path is set)
        resolver.assert_not_awaited()

        pipeline.output_buffer.close()
        await player.stop()

    @patch("tune_server.playback.player.AudioPipeline")
    async def test_resolver_failure_advances_track(self, MockPipeline):
        pipeline = _mock_pipeline(close_immediately=False)
        MockPipeline.return_value = pipeline

        event_bus = EventBus()
        output = AsyncMock()
        type(output).capabilities = PropertyMock(
            return_value=AudioCapabilities(
                formats={AudioFormat.FLAC}, max_sample_rate=44100, max_bit_depth=16,
            )
        )

        player = Player(zone_id=1, event_bus=event_bus)
        player.set_output(output)

        # Resolver returns None (failure) for first track, succeeds for second
        resolver = AsyncMock(side_effect=[None, "https://stream.example.com/track2.flac"])
        player.set_stream_url_resolver(resolver)

        track1 = _streaming_track(source_id="111")
        track2 = _streaming_track(source_id="222")
        track2.title = "Second Track"
        await player.play(tracks=[track1, track2])
        await asyncio.sleep(0.1)

        # Should have advanced past track1 to track2
        assert player.queue.position == 1
        assert resolver.await_count == 2

        pipeline.output_buffer.close()
        await player.stop()


# ---------------------------------------------------------------------------
# 3. Streaming browse endpoints
# ---------------------------------------------------------------------------

class TestStreamingBrowseEndpoints:
    """Test the new browse API endpoints return 503 when service is not available."""

    @pytest.fixture
    async def client(self):
        import httpx
        from tune_server.api.deps import deps
        from tune_server.api.main import create_api_app

        deps.tidal = None
        deps.qobuz = None

        app = create_api_app()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client

        deps.tidal = None
        deps.qobuz = None

    async def test_tidal_track_unavailable(self, client):
        resp = await client.get("/api/v1/streaming/tidal/tracks/123")
        assert resp.status_code == 503

    async def test_tidal_album_unavailable(self, client):
        resp = await client.get("/api/v1/streaming/tidal/albums/456")
        assert resp.status_code == 503

    async def test_tidal_artist_unavailable(self, client):
        resp = await client.get("/api/v1/streaming/tidal/artists/789")
        assert resp.status_code == 503

    async def test_tidal_artist_albums_unavailable(self, client):
        resp = await client.get("/api/v1/streaming/tidal/artists/789/albums")
        assert resp.status_code == 503

    async def test_tidal_artist_tracks_unavailable(self, client):
        resp = await client.get("/api/v1/streaming/tidal/artists/789/tracks")
        assert resp.status_code == 503

    async def test_qobuz_track_unavailable(self, client):
        resp = await client.get("/api/v1/streaming/qobuz/tracks/123")
        assert resp.status_code == 503

    async def test_qobuz_album_unavailable(self, client):
        resp = await client.get("/api/v1/streaming/qobuz/albums/456")
        assert resp.status_code == 503

    async def test_qobuz_artist_unavailable(self, client):
        resp = await client.get("/api/v1/streaming/qobuz/artists/789")
        assert resp.status_code == 503

    async def test_qobuz_artist_albums_unavailable(self, client):
        resp = await client.get("/api/v1/streaming/qobuz/artists/789/albums")
        assert resp.status_code == 503

    async def test_qobuz_artist_tracks_unavailable(self, client):
        resp = await client.get("/api/v1/streaming/qobuz/artists/789/tracks")
        assert resp.status_code == 503

    async def test_tidal_browse_with_mock_service(self, client):
        """With a mock authenticated service, browse endpoints should return data."""
        from tune_server.api.deps import deps

        mock_tidal = AsyncMock()
        mock_tidal.is_authenticated = True
        mock_tidal.get_track = AsyncMock(return_value=Track(
            title="Test Track", source=Source.TIDAL, source_id="123",
        ))
        mock_tidal.get_album = AsyncMock(return_value=Album(
            title="Test Album", source=Source.TIDAL, source_id="456",
        ))
        mock_tidal.get_artist = AsyncMock(return_value=Artist(name="Test Artist"))
        mock_tidal.get_artist_albums = AsyncMock(return_value=[
            Album(title="Album 1", source=Source.TIDAL, source_id="a1"),
        ])
        mock_tidal.get_artist_tracks = AsyncMock(return_value=[
            Track(title="Track 1", source=Source.TIDAL, source_id="t1"),
        ])

        deps.tidal = mock_tidal

        resp = await client.get("/api/v1/streaming/tidal/tracks/123")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Test Track"

        resp = await client.get("/api/v1/streaming/tidal/albums/456")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Test Album"

        resp = await client.get("/api/v1/streaming/tidal/artists/789")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Test Artist"

        resp = await client.get("/api/v1/streaming/tidal/artists/789/albums")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        resp = await client.get("/api/v1/streaming/tidal/artists/789/tracks")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        deps.tidal = None


# ---------------------------------------------------------------------------
# 4. Auth persistence
# ---------------------------------------------------------------------------

class TestAuthPersistence:
    """Test save/restore of streaming auth tokens."""

    async def test_qobuz_save_and_restore(self):
        from tune_server.streaming.qobuz import QobuzService

        db = Database(":memory:")
        await db.connect()

        service = QobuzService()
        # Simulate authenticated state
        service._user_auth_token = "test-token-123"

        await service.save_auth(db)

        # Create new service and restore
        service2 = QobuzService()
        assert not service2.is_authenticated
        restored = await service2.restore_auth(db)
        assert restored
        assert service2._user_auth_token == "test-token-123"

        await db.close()

    async def test_restore_with_no_saved_data(self):
        from tune_server.streaming.qobuz import QobuzService

        db = Database(":memory:")
        await db.connect()

        service = QobuzService()
        restored = await service.restore_auth(db)
        assert not restored
        assert not service.is_authenticated

        await db.close()

    async def test_streaming_auth_table_exists(self):
        db = Database(":memory:")
        await db.connect()

        # Table should exist after schema init
        row = await db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='streaming_auth'"
        )
        assert row is not None

        await db.close()

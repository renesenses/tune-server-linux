from __future__ import annotations

import pytest
from pydantic import ValidationError

from tune_server.models import (
    Album,
    AudioFormat,
    DiscoveredDevice,
    OutputType,
    PlaybackState,
    RepeatMode,
    SearchResult,
    SeekRequest,
    Track,
    VolumeRequest,
    Zone,
    ZoneCreateRequest,
)


class TestTrack:
    def test_track_defaults(self):
        t = Track(title="Some Song")
        assert t.disc_number == 1
        assert t.channels == 2
        assert t.track_number == 0
        assert t.duration_ms == 0
        assert t.id is None
        assert t.file_path is None

    def test_track_serialization_roundtrip(self):
        t = Track(
            id=1,
            title="La nuit je mens",
            format=AudioFormat.FLAC,
            sample_rate=44100,
            bit_depth=16,
        )
        data = t.model_dump()
        restored = Track(**data)
        assert restored == t
        assert restored.format == AudioFormat.FLAC


class TestZone:
    def test_zone_volume_valid(self):
        z = Zone(name="Salon", volume=0.0)
        assert z.volume == 0.0
        z = Zone(name="Salon", volume=1.0)
        assert z.volume == 1.0

    def test_zone_volume_too_low(self):
        with pytest.raises(ValidationError):
            Zone(name="Salon", volume=-0.1)

    def test_zone_volume_too_high(self):
        with pytest.raises(ValidationError):
            Zone(name="Salon", volume=1.5)


class TestSeekRequest:
    def test_seek_non_negative(self):
        s = SeekRequest(position_ms=0)
        assert s.position_ms == 0

    def test_seek_negative_fails(self):
        with pytest.raises(ValidationError):
            SeekRequest(position_ms=-1)


class TestEnums:
    def test_playback_state_enum(self):
        states = {"stopped", "playing", "paused", "buffering"}
        assert {s.value for s in PlaybackState} == states

    def test_output_type_enum(self):
        types = {"local", "dlna", "airplay"}
        assert {t.value for t in OutputType} == types

    def test_repeat_mode_enum(self):
        modes = {"off", "one", "all"}
        assert {m.value for m in RepeatMode} == modes

    def test_audio_format_roundtrip(self):
        for fmt in AudioFormat:
            assert AudioFormat(fmt.value) == fmt


class TestZoneCreateRequest:
    def test_valid_request(self):
        r = ZoneCreateRequest(name="Cuisine", output_type=OutputType.DLNA)
        assert r.name == "Cuisine"
        assert r.output_type == OutputType.DLNA


class TestSearchResult:
    def test_empty(self):
        sr = SearchResult()
        assert sr.tracks == []
        assert sr.albums == []
        assert sr.artists == []


class TestAlbum:
    def test_album_with_artist_name(self):
        a = Album(title="Fantaisie Militaire", artist_name="Alain Bashung", year=1998)
        assert a.artist_name == "Alain Bashung"
        assert a.artist_id is None


class TestDiscoveredDevice:
    def test_discovered_device(self):
        d = DiscoveredDevice(
            id="uuid:123",
            name="Living Room Speaker",
            type=OutputType.DLNA,
            host="192.168.1.50",
            port=1400,
            capabilities={"supports_flac": True},
        )
        assert d.available is True
        assert d.capabilities["supports_flac"] is True


class TestVolumeRequest:
    def test_valid(self):
        v = VolumeRequest(volume=0.75)
        assert v.volume == 0.75

    def test_out_of_range(self):
        with pytest.raises(ValidationError):
            VolumeRequest(volume=1.5)

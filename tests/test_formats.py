from __future__ import annotations

import pytest

from tune_server.audio.formats import (
    AIRPLAY_CAPABILITIES,
    DLNA_CAPABILITIES,
    LOCAL_CAPABILITIES,
    AudioCapabilities,
    can_passthrough,
    choose_output_format,
    ffmpeg_codec_arg,
    ffmpeg_format_arg,
    format_from_extension,
    mime_type_for_format,
)
from tune_server.models import AudioFormat


class TestFormatFromExtension:
    @pytest.mark.parametrize(
        "ext, expected",
        [
            ("flac", AudioFormat.FLAC),
            ("mp3", AudioFormat.MP3),
            ("wav", AudioFormat.WAV),
            ("m4a", AudioFormat.AAC),
            ("aac", AudioFormat.AAC),
            ("ogg", AudioFormat.OGG),
            ("opus", AudioFormat.OPUS),
            ("aiff", AudioFormat.AIFF),
            ("aif", AudioFormat.AIFF),
            ("dsf", AudioFormat.DSD),
            ("dff", AudioFormat.DSD),
            ("wma", AudioFormat.WMA),
            ("alac", AudioFormat.ALAC),
        ],
    )
    def test_known_extensions(self, ext, expected):
        assert format_from_extension(ext) == expected

    def test_unknown_extension(self):
        assert format_from_extension("xyz") is None

    def test_case_insensitive(self):
        assert format_from_extension("FLAC") == AudioFormat.FLAC
        assert format_from_extension("Mp3") == AudioFormat.MP3

    def test_leading_dot_stripped(self):
        assert format_from_extension(".flac") == AudioFormat.FLAC
        assert format_from_extension(".mp3") == AudioFormat.MP3


class TestCanPassthrough:
    def test_flac_to_dlna(self):
        assert can_passthrough(AudioFormat.FLAC, 44100, 16, DLNA_CAPABILITIES) is True

    def test_flac_hires_to_dlna(self):
        assert can_passthrough(AudioFormat.FLAC, 192000, 24, DLNA_CAPABILITIES) is True

    def test_aac_to_local_false(self):
        # LOCAL only supports WAV
        assert can_passthrough(AudioFormat.AAC, 44100, 16, LOCAL_CAPABILITIES) is False

    def test_wav_to_local_true(self):
        assert can_passthrough(AudioFormat.WAV, 44100, 16, LOCAL_CAPABILITIES) is True

    def test_flac_hires_to_airplay_false(self):
        # AirPlay max 44100
        assert can_passthrough(AudioFormat.FLAC, 192000, 24, AIRPLAY_CAPABILITIES) is False

    def test_format_not_in_caps(self):
        assert can_passthrough(AudioFormat.OGG, 44100, 16, DLNA_CAPABILITIES) is False

    def test_sample_rate_exceeds_max(self):
        assert can_passthrough(AudioFormat.WAV, 500000, 16, LOCAL_CAPABILITIES) is False

    def test_bit_depth_exceeds_max(self):
        assert can_passthrough(AudioFormat.WAV, 44100, 64, LOCAL_CAPABILITIES) is False


class TestChooseOutputFormat:
    def test_prefers_flac_for_dlna(self):
        assert choose_output_format(AudioFormat.MP3, DLNA_CAPABILITIES) == AudioFormat.FLAC

    def test_wav_for_local(self):
        # LOCAL only has WAV
        assert choose_output_format(AudioFormat.FLAC, LOCAL_CAPABILITIES) == AudioFormat.WAV

    def test_single_format_caps(self):
        caps = AudioCapabilities(
            formats={AudioFormat.MP3},
            max_sample_rate=48000,
            max_bit_depth=16,
        )
        assert choose_output_format(AudioFormat.FLAC, caps) == AudioFormat.MP3

    def test_airplay_prefers_alac(self):
        # AirPlay has ALAC + AAC; ALAC comes before AAC in preference
        result = choose_output_format(AudioFormat.FLAC, AIRPLAY_CAPABILITIES)
        assert result == AudioFormat.ALAC


class TestFfmpegFormatArg:
    @pytest.mark.parametrize(
        "fmt, expected",
        [
            (AudioFormat.FLAC, "flac"),
            (AudioFormat.WAV, "wav"),
            (AudioFormat.MP3, "mp3"),
            (AudioFormat.AAC, "adts"),
            (AudioFormat.ALAC, "ipod"),
        ],
    )
    def test_mappings(self, fmt, expected):
        assert ffmpeg_format_arg(fmt) == expected

    def test_unknown_defaults_to_flac(self):
        assert ffmpeg_format_arg(AudioFormat.DSD) == "flac"


class TestFfmpegCodecArg:
    @pytest.mark.parametrize(
        "fmt, expected",
        [
            (AudioFormat.FLAC, "flac"),
            (AudioFormat.WAV, "pcm_s16le"),
            (AudioFormat.MP3, "libmp3lame"),
            (AudioFormat.AAC, "aac"),
            (AudioFormat.ALAC, "alac"),
        ],
    )
    def test_mappings(self, fmt, expected):
        assert ffmpeg_codec_arg(fmt) == expected


class TestMimeType:
    @pytest.mark.parametrize(
        "fmt, expected",
        [
            (AudioFormat.FLAC, "audio/flac"),
            (AudioFormat.MP3, "audio/mpeg"),
            (AudioFormat.WAV, "audio/wav"),
            (AudioFormat.AAC, "audio/aac"),
            (AudioFormat.ALAC, "audio/mp4"),
            (AudioFormat.OGG, "audio/ogg"),
            (AudioFormat.OPUS, "audio/opus"),
            (AudioFormat.AIFF, "audio/aiff"),
        ],
    )
    def test_known_formats(self, fmt, expected):
        assert mime_type_for_format(fmt) == expected

    def test_unknown_format_fallback(self):
        assert mime_type_for_format(AudioFormat.DSD) == "application/octet-stream"
        assert mime_type_for_format(AudioFormat.WMA) == "application/octet-stream"

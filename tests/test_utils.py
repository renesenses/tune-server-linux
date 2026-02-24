from __future__ import annotations

from tune_server.utils.audio_utils import (
    check_ffmpeg,
    format_duration,
    ms_to_pcm_bytes,
    pcm_bytes_per_second,
)


def test_format_duration_minutes_seconds():
    assert format_duration(185000) == "3:05"


def test_format_duration_with_hours():
    assert format_duration(3661000) == "1:01:01"


def test_format_duration_zero():
    assert format_duration(0) == "0:00"


def test_pcm_bytes_per_second():
    assert pcm_bytes_per_second(44100, 16, 2) == 176400


def test_ms_to_pcm_bytes():
    assert ms_to_pcm_bytes(1000, 44100, 16, 2) == 176400


def test_check_ffmpeg():
    result = check_ffmpeg()
    assert isinstance(result, bool)


def test_settings_defaults():
    from tune_server.config import Settings

    s = Settings()
    assert s.api_port == 8888
    assert s.stream_port == 8080
    assert s.db_path == "tune_server.db"
    assert s.scan_on_startup is True
    assert s.default_output_format == "flac"


def test_settings_env_prefix():
    from tune_server.config import Settings

    assert Settings.model_config["env_prefix"] == "TUNE_"

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import structlog

logger = structlog.get_logger()


def check_ffmpeg() -> bool:
    """Check if FFmpeg is available (bundled or system PATH)."""
    from tune_server.config import settings
    return shutil.which(settings.ffmpeg_path) is not None or Path(settings.ffmpeg_path).is_file()


def check_ffprobe() -> bool:
    """Check if ffprobe is available (bundled or system PATH)."""
    from tune_server.config import settings
    return shutil.which(settings.ffprobe_path) is not None or Path(settings.ffprobe_path).is_file()


def format_duration(ms: int) -> str:
    """Format milliseconds to HH:MM:SS or MM:SS."""
    seconds = ms // 1000
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def pcm_bytes_per_second(sample_rate: int, bit_depth: int, channels: int) -> int:
    return sample_rate * (bit_depth // 8) * channels


def ms_to_pcm_bytes(ms: int, sample_rate: int, bit_depth: int, channels: int) -> int:
    bps = pcm_bytes_per_second(sample_rate, bit_depth, channels)
    return int(bps * ms / 1000)

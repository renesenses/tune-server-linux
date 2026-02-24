from __future__ import annotations

import asyncio
import shutil

import structlog

logger = structlog.get_logger()


def check_ffmpeg() -> bool:
    """Check if FFmpeg is available on the system."""
    return shutil.which("ffmpeg") is not None


def check_ffprobe() -> bool:
    """Check if ffprobe is available on the system."""
    return shutil.which("ffprobe") is not None


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

"""Audio analysis: BPM detection and waveform generation via FFmpeg + numpy."""

from __future__ import annotations

import asyncio
import math

import numpy as np
import structlog

logger = structlog.get_logger()


async def _ffmpeg_pcm(
    file_path: str,
    sample_rate: int = 22050,
    channels: int = 1,
    seek_s: float = 0,
    duration_s: float = 0,
) -> bytes:
    """Extract PCM audio via FFmpeg subprocess.

    Returns raw signed 16-bit little-endian PCM bytes.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if seek_s > 0:
        cmd += ["-ss", str(seek_s)]
    cmd += ["-i", file_path]
    if duration_s > 0:
        cmd += ["-t", str(duration_s)]
    cmd += ["-ac", str(channels), "-ar", str(sample_rate), "-f", "s16le", "pipe:1"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err_msg = stderr.decode(errors="replace").strip()
        logger.warning("ffmpeg_pcm_error", file=file_path, error=err_msg[:200])
        return b""
    return stdout


async def _get_duration(file_path: str) -> float:
    """Get file duration in seconds via ffprobe."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        file_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        return float(stdout.decode().strip())
    except (ValueError, TypeError):
        return 0.0


async def measure_loudness(file_path: str) -> float | None:
    """Measure integrated loudness in LUFS using FFmpeg ebur128 filter.
    Returns LUFS value (typically -14 to -23 for music).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", file_path, "-af", "ebur128=peak=true",
            "-f", "null", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        # Parse "I: -14.0 LUFS" from ebur128 output
        for line in stderr.decode().split('\n'):
            if 'I:' in line and 'LUFS' in line:
                parts = line.strip().split()
                for i, p in enumerate(parts):
                    if p == 'I:':
                        return float(parts[i + 1])
    except Exception:
        logger.debug("loudness_measure_failed", file=file_path)
    return None


async def detect_trailing_silence(file_path: str, threshold_db: float = -40.0) -> float:
    """Detect trailing silence duration in seconds.
    Returns the number of seconds of silence at the end of the track.
    """
    try:
        # Use FFmpeg silencedetect filter
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", file_path,
            "-af", f"silencedetect=noise={threshold_db}dB:d=0.5",
            "-f", "null", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        # Parse silence_end from stderr
        lines = stderr.decode()
        silence_duration = 0.0
        for line in lines.split('\n'):
            if 'silence_end' in line:
                # Format: [silencedetect @ 0x...] silence_end: 123.456 | silence_duration: 2.345
                dur_parts = line.split('silence_duration:')
                if len(dur_parts) > 1:
                    silence_duration = float(dur_parts[1].strip())

        return silence_duration
    except Exception:
        logger.debug("silence_detect_failed", file=file_path)
        return 0.0


async def detect_bpm(file_path: str, duration: int = 30) -> float | None:
    """Detect BPM using FFmpeg PCM extraction + numpy autocorrelation.

    Extracts 30s from the middle of the track, converts to mono 22050Hz,
    computes energy envelope, autocorrelation, and finds dominant tempo.
    """
    sample_rate = 22050

    try:
        file_duration = await _get_duration(file_path)
        if file_duration <= 0:
            logger.warning("bpm_no_duration", file=file_path)
            return None

        # Seek to middle of track
        start = max(0.0, file_duration / 2 - duration / 2)

        pcm_bytes = await _ffmpeg_pcm(
            file_path,
            sample_rate=sample_rate,
            channels=1,
            seek_s=start,
            duration_s=duration,
        )
        if not pcm_bytes:
            return None

        # Convert to numpy int16
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
        if len(samples) < sample_rate * 2:
            logger.warning("bpm_too_short", file=file_path, samples=len(samples))
            return None

        # Compute energy envelope: rectify + low-pass (moving average)
        envelope = np.abs(samples)
        window_size = 2048
        kernel = np.ones(window_size) / window_size
        envelope = np.convolve(envelope, kernel, mode="same")

        # Remove DC offset
        envelope = envelope - np.mean(envelope)

        # Autocorrelation of envelope (use only second half for positive lags)
        corr = np.correlate(envelope, envelope, mode="full")
        corr = corr[len(corr) // 2 :]

        # BPM range: 60-200 BPM
        # lag for 60 BPM = 60 * sample_rate / 60 = sample_rate = 22050
        # lag for 200 BPM = 60 * sample_rate / 200 = 6615
        min_lag = int(60 * sample_rate / 200)  # 200 BPM
        max_lag = int(60 * sample_rate / 60)  # 60 BPM

        if max_lag >= len(corr):
            max_lag = len(corr) - 1
        if min_lag >= max_lag:
            return None

        # Find the strongest peak in the autocorrelation
        search_region = corr[min_lag:max_lag]
        peak_idx = np.argmax(search_region) + min_lag

        # Convert lag to BPM
        bpm = 60.0 * sample_rate / peak_idx

        # Round to nearest integer
        bpm = round(bpm)

        # Sanity check
        if bpm < 40 or bpm > 220:
            logger.debug("bpm_out_of_range", file=file_path, bpm=bpm)
            return None

        logger.info("bpm_detected", file=file_path, bpm=bpm)
        return float(bpm)

    except Exception:
        logger.exception("bpm_detection_error", file=file_path)
        return None


async def generate_waveform(file_path: str, points: int = 800) -> list[float]:
    """Generate waveform envelope (RMS per frame), normalized 0.0-1.0.

    Returns ~800 float values representing the audio amplitude over time.
    """
    sample_rate = 22050

    try:
        pcm_bytes = await _ffmpeg_pcm(
            file_path,
            sample_rate=sample_rate,
            channels=1,
        )
        if not pcm_bytes:
            return []

        # Convert to numpy int16
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
        if len(samples) < points:
            return []

        # Divide into equal frames
        frame_size = len(samples) // points
        rms_values = []

        for i in range(points):
            start = i * frame_size
            end = start + frame_size
            frame = samples[start:end]
            rms = math.sqrt(np.mean(frame ** 2))
            rms_values.append(rms)

        # Normalize to 0.0-1.0
        max_rms = max(rms_values) if rms_values else 1.0
        if max_rms > 0:
            rms_values = [v / max_rms for v in rms_values]

        return [round(v, 4) for v in rms_values]

    except Exception:
        logger.exception("waveform_generation_error", file=file_path)
        return []

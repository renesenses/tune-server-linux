from __future__ import annotations

import asyncio
import time

import numpy as np
import sounddevice as sd
import structlog

from tune_server.audio.formats import LOCAL_CAPABILITIES, AudioCapabilities
from tune_server.models import AudioStreamInfo, Track
from tune_server.outputs.base import OutputTarget

logger = structlog.get_logger()


def list_local_audio_devices() -> list[dict]:
    """Return the current list of local audio output devices (refreshed each call)."""
    try:
        devices = sd.query_devices()
        result = []
        for i, d in enumerate(devices):
            if d["max_output_channels"] > 0:
                result.append({"index": i, "name": d["name"], "channels": d["max_output_channels"],
                               "sample_rate": int(d["default_samplerate"])})
        return result
    except Exception:
        logger.exception("list_local_audio_devices_error")
        return []


def _find_device_index(device_name: str) -> int | None:
    """Find a device index by name with progressive matching:
    1. Exact match (case-insensitive)
    2. Substring match (case-insensitive)
    3. Partial word match (any word in the requested name appears in the device name)
    Returns the sounddevice index or None.
    """
    devices = sd.query_devices()
    target = device_name.lower()
    target_words = [w for w in target.split() if len(w) > 2]

    # Pass 1: exact match
    for i, d in enumerate(devices):
        if d["max_output_channels"] > 0 and d["name"].lower() == target:
            logger.debug("device_match_exact", requested=device_name, matched=d["name"], index=i)
            return i

    # Pass 2: substring match (either direction)
    for i, d in enumerate(devices):
        if d["max_output_channels"] > 0:
            dname = d["name"].lower()
            if target in dname or dname in target:
                logger.debug("device_match_substring", requested=device_name, matched=d["name"], index=i)
                return i

    # Pass 3: partial word match — at least one significant word from request in device name
    if target_words:
        for i, d in enumerate(devices):
            if d["max_output_channels"] > 0:
                dname = d["name"].lower()
                if any(w in dname for w in target_words):
                    logger.debug("device_match_partial", requested=device_name, matched=d["name"], index=i)
                    return i

    logger.warning("device_not_found", requested=device_name,
                    available=[d["name"] for d in devices if d["max_output_channels"] > 0])
    return None


class LocalOutput(OutputTarget):
    """Local audio output using sounddevice (PortAudio)."""

    def __init__(self, device_name: str | None = None) -> None:
        self._device_name = device_name
        self._stream: sd.OutputStream | None = None
        self._stream_info: AudioStreamInfo | None = None
        self._volume: float = 1.0
        self._paused = False
        self._available = True
        self._recovery_attempts = 0
        self._start_time: float = 0.0
        self._elapsed_before_pause: float = 0.0
        self._exclusive: bool = False
        # Log available devices on first instantiation for diagnostics
        available = list_local_audio_devices()
        logger.info("local_output_init", requested_device=device_name,
                     available_devices=[d["name"] for d in available])

    @property
    def name(self) -> str:
        return self._device_name or "Local Output"

    @property
    def capabilities(self) -> AudioCapabilities:
        return LOCAL_CAPABILITIES

    @property
    def is_available(self) -> bool:
        return self._available

    async def start(self, stream_info: AudioStreamInfo, track: Track | None = None) -> None:
        await self.stop()
        self._stream_info = stream_info
        self._paused = False
        self._start_time = time.monotonic()
        self._elapsed_before_pause = 0.0

        dtype_map = {
            8: "int8",
            16: "int16",
            24: "float32",  # 24-bit → float32 for universal DAC compatibility
            32: "float32",
        }
        dtype = dtype_map.get(stream_info.bit_depth, "int16")

        try:
            from tune_server.config import settings as _s
            device = None
            if self._device_name:
                device = _find_device_index(self._device_name)

            # Exclusive mode: use ALSA hw: device directly (bypass PulseAudio/PipeWire mixer)
            exclusive = _s.local_exclusive_mode
            latency_s = _s.local_latency_ms / 1000.0
            blocksize = max(256, int(stream_info.sample_rate * latency_s / 4))

            extra_settings = None
            if exclusive and device is not None:
                try:
                    dev_info = sd.query_devices(device)
                    dev_name = dev_info.get("name", "")
                    # Try to find ALSA hw: device for exclusive access
                    hostapi = sd.query_hostapis(dev_info.get("hostapi", 0))
                    if hostapi.get("name", "").lower() == "alsa":
                        extra_settings = sd.AsioSettings(channel_selectors=[0, 1]) if hasattr(sd, "AsioSettings") else None
                        logger.info("local_exclusive_mode", device=dev_name)
                except Exception:
                    logger.debug("local_exclusive_mode_detection_failed")

            out_rate = stream_info.sample_rate
            self._resample_ratio: float | None = None

            try:
                self._stream = sd.OutputStream(
                    samplerate=out_rate,
                    channels=stream_info.channels,
                    dtype=dtype,
                    device=device,
                    blocksize=blocksize,
                    latency="low" if exclusive else "high",
                    extra_settings=extra_settings,
                )
            except sd.PortAudioError:
                dev_info = sd.query_devices(device if device is not None else sd.default.device[1])
                default_rate = int(dev_info.get("default_samplerate", 48000))
                # Try progressively lower rates from the source rate down to
                # find the highest rate the device actually supports. This
                # helps USB DACs (e.g. Chord Mojo) that support high rates but
                # whose Windows default is stuck at 44.1kHz.
                _candidate_rates = sorted(
                    [r for r in [768000, 384000, 352800, 192000, 176400, 96000, 88200, 48000, 44100]
                     if r < out_rate and r >= default_rate],
                    reverse=True,
                )
                best_rate = default_rate
                for candidate in _candidate_rates:
                    try:
                        test_stream = sd.OutputStream(
                            samplerate=candidate,
                            channels=stream_info.channels,
                            dtype=dtype,
                            device=device,
                            blocksize=blocksize,
                            latency="low" if exclusive else "high",
                            extra_settings=extra_settings,
                        )
                        test_stream.close()
                        best_rate = candidate
                        break
                    except sd.PortAudioError:
                        continue
                logger.warning("local_output_resample",
                               source_rate=out_rate, device_rate=best_rate,
                               default_rate=default_rate)
                self._resample_ratio = best_rate / out_rate
                out_rate = best_rate
                blocksize = max(256, int(out_rate * latency_s / 4))
                self._stream = sd.OutputStream(
                    samplerate=out_rate,
                    channels=stream_info.channels,
                    dtype=dtype,
                    device=device,
                    blocksize=blocksize,
                    latency="low" if exclusive else "high",
                    extra_settings=extra_settings,
                )

            self._stream.start()
            self._available = True
            self._exclusive = exclusive
            logger.info(
                "local_output_started",
                sample_rate=out_rate,
                source_rate=stream_info.sample_rate,
                channels=stream_info.channels,
                bit_depth=stream_info.bit_depth,
                resampled=self._resample_ratio is not None,
                exclusive=exclusive,
                blocksize=blocksize,
                latency_ms=_s.local_latency_ms,
            )
        except Exception:
            logger.exception("local_output_start_error")
            self._available = False

    async def write(self, data: bytes) -> None:
        if not self._stream or self._paused:
            return

        try:
            info = self._stream_info
            if not info:
                return

            # Convert bytes to numpy array (trim trailing bytes to align)
            if info.bit_depth <= 16:
                trim = len(data) % 2
                arr = np.frombuffer(data[:len(data) - trim] if trim else data, dtype=np.int16)
            else:
                trim = len(data) % 4
                arr = np.frombuffer(data[:len(data) - trim] if trim else data, dtype=np.int32)
                # Convert int32 → float32 [-1.0, 1.0] for DAC compatibility
                arr = arr.astype(np.float32) / 2147483648.0

            # Apply volume (skip in exclusive mode for bit-perfect output)
            if self._volume < 1.0 and not self._exclusive:
                arr = (arr * self._volume).astype(arr.dtype)

            # Reshape for channels
            if info.channels > 1 and len(arr) >= info.channels:
                remainder = len(arr) % info.channels
                if remainder:
                    arr = arr[:-remainder]
                arr = arr.reshape(-1, info.channels)

            # Resample if device doesn't support the source sample rate
            if self._resample_ratio is not None and self._resample_ratio != 1.0:
                n_out = int(len(arr) * self._resample_ratio)
                if n_out > 0:
                    indices = np.linspace(0, len(arr) - 1, n_out)
                    if arr.ndim == 2:
                        arr = np.column_stack([
                            np.interp(indices, np.arange(len(arr)), arr[:, ch].astype(np.float64)).astype(arr.dtype)
                            for ch in range(arr.shape[1])
                        ])
                    else:
                        arr = np.interp(indices, np.arange(len(arr)), arr.astype(np.float64)).astype(arr.dtype)

            await asyncio.to_thread(self._stream.write, arr)
        except sd.PortAudioError:
            self._recovery_attempts += 1
            logger.warning("local_output_write_error_recovering", attempt=self._recovery_attempts)
            if self._recovery_attempts > 3:
                self._available = False
                raise IOError("Local output recovery failed after 3 attempts")
            # Attempt to recover by restarting the stream
            if self._stream_info:
                await asyncio.sleep(0.5 * self._recovery_attempts)
                try:
                    await self.start(self._stream_info)
                    self._recovery_attempts = 0
                except Exception:
                    logger.exception("local_output_recovery_failed")
        except Exception:
            logger.exception("local_output_write_error")

    async def flush(self) -> None:
        pass

    async def pause(self) -> None:
        if not self._paused:
            self._elapsed_before_pause += time.monotonic() - self._start_time
        self._paused = True
        if self._stream:
            self._stream.stop()

    async def resume(self) -> None:
        self._paused = False
        self._start_time = time.monotonic()
        if self._stream:
            self._stream.start()

    async def stop(self) -> None:
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.debug("local_output_stop_error", error=str(e))
            self._stream = None

    async def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))

    async def get_position_ms(self) -> int:
        """Return elapsed playback time in milliseconds."""
        if self._paused:
            return int(self._elapsed_before_pause * 1000)
        if self._start_time > 0:
            elapsed = self._elapsed_before_pause + (time.monotonic() - self._start_time)
            return int(elapsed * 1000)
        return -1

    async def close(self) -> None:
        await self.stop()

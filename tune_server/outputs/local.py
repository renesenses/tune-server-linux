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
                devices = sd.query_devices()
                for i, d in enumerate(devices):
                    if self._device_name.lower() in d["name"].lower() and d["max_output_channels"] > 0:
                        device = i
                        break

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
                logger.warning("local_output_resample",
                               source_rate=out_rate, device_rate=default_rate)
                self._resample_ratio = default_rate / out_rate
                out_rate = default_rate
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

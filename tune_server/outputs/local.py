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
        self._start_time: float = 0.0
        self._elapsed_before_pause: float = 0.0

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
            24: "int32",  # 24-bit stored as 32-bit
            32: "int32",
        }
        dtype = dtype_map.get(stream_info.bit_depth, "int16")

        try:
            device = None
            if self._device_name:
                devices = sd.query_devices()
                for i, d in enumerate(devices):
                    if self._device_name.lower() in d["name"].lower() and d["max_output_channels"] > 0:
                        device = i
                        break

            self._stream = sd.OutputStream(
                samplerate=stream_info.sample_rate,
                channels=stream_info.channels,
                dtype=dtype,
                device=device,
                blocksize=1024,
            )
            self._stream.start()
            self._available = True
            logger.info(
                "local_output_started",
                sample_rate=stream_info.sample_rate,
                channels=stream_info.channels,
                bit_depth=stream_info.bit_depth,
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

            # Convert bytes to numpy array
            if info.bit_depth <= 16:
                arr = np.frombuffer(data, dtype=np.int16)
            else:
                arr = np.frombuffer(data, dtype=np.int32)

            # Apply volume
            if self._volume < 1.0:
                arr = (arr * self._volume).astype(arr.dtype)

            # Reshape for channels
            if info.channels > 1 and len(arr) >= info.channels:
                remainder = len(arr) % info.channels
                if remainder:
                    arr = arr[:-remainder]
                arr = arr.reshape(-1, info.channels)

            await asyncio.to_thread(self._stream.write, arr)
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
            except Exception:
                pass
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

"""PCM AudioMixer — mix two audio streams with independent gain control."""

from __future__ import annotations

import numpy as np
import structlog

logger = structlog.get_logger()


class AudioMixer:
    """Mix two PCM audio streams with independent gain control.

    Operates on raw PCM bytes (s16le or s32le), converts to numpy arrays,
    applies gain, sums, clips, and returns the mixed result as bytes.
    """

    def __init__(self, sample_rate: int, bit_depth: int, channels: int) -> None:
        self._sample_rate = sample_rate
        self._bit_depth = bit_depth
        self._channels = channels

        if bit_depth <= 16:
            self._dtype = np.int16
            self._max_val = np.iinfo(np.int16).max
            self._min_val = np.iinfo(np.int16).min
            self._bytes_per_sample = 2
        else:
            self._dtype = np.int32
            self._max_val = np.iinfo(np.int32).max
            self._min_val = np.iinfo(np.int32).min
            self._bytes_per_sample = 4

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def bit_depth(self) -> int:
        return self._bit_depth

    @property
    def channels(self) -> int:
        return self._channels

    def _bytes_to_array(self, data: bytes) -> np.ndarray:
        """Convert raw PCM bytes to a numpy array, trimming any trailing partial sample."""
        bps = self._bytes_per_sample
        trim = len(data) % bps
        if trim:
            data = data[: len(data) - trim]
        return np.frombuffer(data, dtype=self._dtype).copy()

    def mix(
        self,
        chunk_a: bytes | None,
        chunk_b: bytes | None,
        gain_a: float,
        gain_b: float,
    ) -> bytes:
        """Mix two PCM chunks with gain. If one is None, return the other scaled.

        Args:
            chunk_a: Raw PCM bytes from deck A (or None if no data).
            chunk_b: Raw PCM bytes from deck B (or None if no data).
            gain_a: Gain multiplier for deck A (0.0 to 1.0).
            gain_b: Gain multiplier for deck B (0.0 to 1.0).

        Returns:
            Mixed PCM bytes, clipped to avoid overflow.
        """
        if chunk_a is None and chunk_b is None:
            return b""

        if chunk_a is None:
            arr_b = self._bytes_to_array(chunk_b)
            if gain_b != 1.0:
                mixed = (arr_b.astype(np.float64) * gain_b)
                mixed = np.clip(mixed, self._min_val, self._max_val).astype(self._dtype)
                return mixed.tobytes()
            return arr_b.tobytes()

        if chunk_b is None:
            arr_a = self._bytes_to_array(chunk_a)
            if gain_a != 1.0:
                mixed = (arr_a.astype(np.float64) * gain_a)
                mixed = np.clip(mixed, self._min_val, self._max_val).astype(self._dtype)
                return mixed.tobytes()
            return arr_a.tobytes()

        arr_a = self._bytes_to_array(chunk_a)
        arr_b = self._bytes_to_array(chunk_b)

        # Pad the shorter array with zeros to match lengths
        if len(arr_a) < len(arr_b):
            arr_a = np.pad(arr_a, (0, len(arr_b) - len(arr_a)), mode="constant")
        elif len(arr_b) < len(arr_a):
            arr_b = np.pad(arr_b, (0, len(arr_a) - len(arr_b)), mode="constant")

        # Mix in float64 to avoid overflow, then clip back to integer range
        mixed = arr_a.astype(np.float64) * gain_a + arr_b.astype(np.float64) * gain_b
        mixed = np.clip(mixed, self._min_val, self._max_val).astype(self._dtype)

        return mixed.tobytes()

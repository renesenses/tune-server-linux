"""Per-renderer DLNA buffer stability tracker.

Tracks disconnections, underruns, and playback interruptions per device UUID.
Automatically adjusts buffer size for problematic renderers: stable devices
keep a small 2s buffer for low latency, while unstable devices get up to 10s.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger()

# --- Constants ---

DEFAULT_BUFFER_S: float = 2.0
MIN_BUFFER_S: float = 1.0
MAX_BUFFER_S: float = 10.0
BUFFER_STEP_S: float = 1.0          # increase per adjustment
UNDERRUN_THRESHOLD: int = 3         # N events in the time window
UNDERRUN_WINDOW_S: float = 600.0    # 10 minutes
STABILITY_WINDOW_S: float = 1800.0  # 30 min of stability → decrease buffer


class EventKind(str, Enum):
    DISCONNECTION = "disconnection"
    UNDERRUN = "underrun"
    INTERRUPTION = "interruption"


@dataclass
class _StabilityEvent:
    kind: EventKind
    timestamp: float


@dataclass
class DeviceBufferStats:
    """Stability metrics and buffer state for a single DLNA renderer."""

    device_id: str
    device_name: str = ""
    buffer_s: float = DEFAULT_BUFFER_S
    manual_override: bool = False       # True if user set buffer manually

    # Counters (lifetime)
    total_disconnections: int = 0
    total_underruns: int = 0
    total_interruptions: int = 0
    total_adjustments: int = 0

    # Recent events for windowed analysis
    _recent_events: list[_StabilityEvent] = field(default_factory=list)
    _last_adjustment_time: float = 0.0
    _last_stable_check: float = field(default_factory=time.monotonic)

    def record_event(self, kind: EventKind) -> float | None:
        """Record a stability event. Returns new buffer_s if adjusted, else None."""
        now = time.monotonic()
        self._recent_events.append(_StabilityEvent(kind=kind, timestamp=now))

        if kind == EventKind.DISCONNECTION:
            self.total_disconnections += 1
        elif kind == EventKind.UNDERRUN:
            self.total_underruns += 1
        elif kind == EventKind.INTERRUPTION:
            self.total_interruptions += 1

        # Prune old events outside the window
        cutoff = now - UNDERRUN_WINDOW_S
        self._recent_events = [e for e in self._recent_events if e.timestamp >= cutoff]

        # Check if we need to increase buffer
        if not self.manual_override:
            return self._maybe_increase_buffer(now)
        return None

    def _maybe_increase_buffer(self, now: float) -> float | None:
        """Increase buffer if recent event count exceeds threshold."""
        cutoff = now - UNDERRUN_WINDOW_S
        recent_count = sum(1 for e in self._recent_events if e.timestamp >= cutoff)

        if recent_count >= UNDERRUN_THRESHOLD and self.buffer_s < MAX_BUFFER_S:
            old = self.buffer_s
            self.buffer_s = min(self.buffer_s + BUFFER_STEP_S, MAX_BUFFER_S)
            self.total_adjustments += 1
            self._last_adjustment_time = now
            self._last_stable_check = now  # reset stability timer
            logger.info(
                "dlna_buffer_adjusted",
                device=self.device_name or self.device_id,
                old=f"{old}s",
                new=f"{self.buffer_s}s",
                reason=f"{recent_count}_events_in_{int(UNDERRUN_WINDOW_S / 60)}min",
            )
            return self.buffer_s
        return None

    def check_stability_decrease(self) -> float | None:
        """If device has been stable for STABILITY_WINDOW_S, decrease buffer.

        Called periodically. Returns new buffer_s if decreased, else None.
        """
        if self.manual_override:
            return None
        if self.buffer_s <= DEFAULT_BUFFER_S:
            return None

        now = time.monotonic()
        cutoff = now - STABILITY_WINDOW_S
        recent_count = sum(1 for e in self._recent_events if e.timestamp >= cutoff)

        if recent_count == 0 and (now - self._last_stable_check) >= STABILITY_WINDOW_S:
            old = self.buffer_s
            self.buffer_s = max(self.buffer_s - BUFFER_STEP_S, DEFAULT_BUFFER_S)
            self._last_stable_check = now
            if self.buffer_s != old:
                self.total_adjustments += 1
                logger.info(
                    "dlna_buffer_adjusted",
                    device=self.device_name or self.device_id,
                    old=f"{old}s",
                    new=f"{self.buffer_s}s",
                    reason="stable_for_30min",
                )
                return self.buffer_s
        return None

    def set_manual_buffer(self, buffer_s: float) -> None:
        """Manually set buffer size, disabling auto-adjustment."""
        buffer_s = max(MIN_BUFFER_S, min(MAX_BUFFER_S, buffer_s))
        old = self.buffer_s
        self.buffer_s = buffer_s
        self.manual_override = True
        logger.info(
            "dlna_buffer_manual_set",
            device=self.device_name or self.device_id,
            old=f"{old}s",
            new=f"{buffer_s}s",
        )

    def clear_manual_override(self) -> None:
        """Re-enable auto-adjustment."""
        self.manual_override = False
        logger.info(
            "dlna_buffer_manual_cleared",
            device=self.device_name or self.device_id,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize stats for the API response."""
        now = time.monotonic()
        cutoff = now - UNDERRUN_WINDOW_S
        recent = [e for e in self._recent_events if e.timestamp >= cutoff]
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "buffer_s": self.buffer_s,
            "manual_override": self.manual_override,
            "total_disconnections": self.total_disconnections,
            "total_underruns": self.total_underruns,
            "total_interruptions": self.total_interruptions,
            "total_adjustments": self.total_adjustments,
            "recent_events_in_window": len(recent),
            "window_minutes": int(UNDERRUN_WINDOW_S / 60),
        }


class DlnaBufferStatsRegistry:
    """Global registry of per-device buffer stability stats (in-memory)."""

    def __init__(self) -> None:
        self._devices: dict[str, DeviceBufferStats] = {}

    def get_or_create(self, device_id: str, device_name: str = "") -> DeviceBufferStats:
        """Get existing stats or create new entry for a device."""
        if device_id not in self._devices:
            self._devices[device_id] = DeviceBufferStats(
                device_id=device_id,
                device_name=device_name,
            )
        stats = self._devices[device_id]
        if device_name and not stats.device_name:
            stats.device_name = device_name
        return stats

    def get(self, device_id: str) -> DeviceBufferStats | None:
        return self._devices.get(device_id)

    def record_event(self, device_id: str, kind: EventKind, device_name: str = "") -> float | None:
        """Record an event for a device. Returns new buffer_s if adjusted."""
        stats = self.get_or_create(device_id, device_name)
        return stats.record_event(kind)

    def get_buffer_s(self, device_id: str) -> float:
        """Get the current buffer size for a device (default if unknown)."""
        stats = self._devices.get(device_id)
        return stats.buffer_s if stats else DEFAULT_BUFFER_S

    def all_stats(self) -> list[dict[str, Any]]:
        """Return stats for all tracked devices."""
        return [s.to_dict() for s in self._devices.values()]

    def check_all_stability(self) -> None:
        """Periodic check: decrease buffers for devices that have been stable."""
        for stats in self._devices.values():
            stats.check_stability_decrease()


# Module-level singleton — imported by DlnaOutput, API routes, etc.
dlna_buffer_registry = DlnaBufferStatsRegistry()

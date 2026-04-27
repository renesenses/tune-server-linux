"""In-memory ring buffer of recent error/warning log events.

Hooks into structlog as a processor: every event with level >= warning is
captured here so the diagnostics endpoint can return the last N entries.
Useful for remote debugging — testers can curl /api/v1/system/diagnostics
and we get the recent errors without ssh/journalctl access.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any


_BUFFER_SIZE = 50

# Single global ring shared by every logger via the structlog processor.
_buffer: deque[dict[str, Any]] = deque(maxlen=_BUFFER_SIZE)


def capture_processor(_logger, method_name: str, event_dict: dict) -> dict:
    """structlog processor: snapshot warning/error/critical events.

    Must return event_dict unchanged so the rest of the pipeline runs.
    """
    if method_name in ("warning", "error", "critical", "exception"):
        # Keep a small, JSON-friendly snapshot — strip non-serializable values.
        snapshot = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": method_name,
            "event": event_dict.get("event", ""),
        }
        for key, value in event_dict.items():
            if key in ("event", "timestamp", "level"):
                continue
            try:
                # Truncate big values (tracebacks, etc.) to keep the buffer light.
                snapshot[key] = str(value)[:500]
            except Exception:
                snapshot[key] = "<unserializable>"
        _buffer.append(snapshot)
    return event_dict


def recent_errors(limit: int | None = None) -> list[dict[str, Any]]:
    """Return the most recent captured events, newest last."""
    if limit is None or limit >= len(_buffer):
        return list(_buffer)
    return list(_buffer)[-limit:]


def clear() -> None:
    """Empty the buffer (useful for tests)."""
    _buffer.clear()

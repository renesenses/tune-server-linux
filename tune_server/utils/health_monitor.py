"""Background health monitor for Tune Server.

Periodically checks system resources (memory, CPU, disk), playback state
(stalled zones, disconnected renderers), and error rates. Alerts are
stored in a ring buffer and pushed to WebSocket clients in real-time
via the EventBus.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import structlog

from tune_server.config import settings
from tune_server.event_bus import Event, EventBus

logger = structlog.get_logger()

try:
    import psutil  # type: ignore[import-untyped]
    _HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False

# ---------------------------------------------------------------------------
# Configuration defaults (overridable via TUNE_ env vars)
# ---------------------------------------------------------------------------

_MONITOR_ENABLED = os.environ.get("TUNE_HEALTH_MONITOR_ENABLED", "true").lower() in ("true", "1", "yes")
_MONITOR_INTERVAL = int(os.environ.get("TUNE_HEALTH_MONITOR_INTERVAL", "60"))
_MEMORY_WARNING_MB = int(os.environ.get("TUNE_HEALTH_MEMORY_WARNING_MB", "500"))
_MEMORY_CRITICAL_MB = int(os.environ.get("TUNE_HEALTH_MEMORY_CRITICAL_MB", "1024"))
_DISK_WARNING_GB = int(os.environ.get("TUNE_HEALTH_DISK_WARNING_GB", "5"))
_DISK_CRITICAL_GB = int(os.environ.get("TUNE_HEALTH_DISK_CRITICAL_GB", "1"))

_MAX_ALERTS = 50
_STALL_THRESHOLD_S = 30  # zone "playing" but position unchanged for N seconds
_ERROR_SPIKE_WINDOW = 300  # 5 minutes
_ERROR_SPIKE_THRESHOLD = 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HealthMonitor:
    """Periodically checks server health and emits alerts."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._task: asyncio.Task | None = None
        self._start_time = time.monotonic()
        self._alerts: deque[dict[str, Any]] = deque(maxlen=_MAX_ALERTS)

        # Playback stall tracking: zone_id -> (last_known_position_ms, monotonic timestamp)
        self._zone_positions: dict[int, tuple[int, float]] = {}

        # Last check results (exposed via the API)
        self._last_checks: dict[str, dict[str, Any]] = {}
        self._last_status: str = "ok"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self._start_time)

    @property
    def status(self) -> str:
        return self._last_status

    @property
    def checks(self) -> dict[str, dict[str, Any]]:
        return dict(self._last_checks)

    @property
    def alerts(self) -> list[dict[str, Any]]:
        return list(self._alerts)

    async def start(self) -> None:
        if not _MONITOR_ENABLED:
            logger.info("health_monitor_disabled")
            return
        self._start_time = time.monotonic()
        self._task = asyncio.create_task(self._loop())
        logger.info("health_monitor_started", interval=_MONITOR_INTERVAL)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("health_monitor_stopped")

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        try:
            while True:
                try:
                    await self._run_checks()
                except Exception:
                    logger.exception("health_monitor_check_error")
                await asyncio.sleep(_MONITOR_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def _run_checks(self) -> None:
        checks: dict[str, dict[str, Any]] = {}
        worst = "ok"

        # ---- Memory ----
        checks["memory"] = await self._check_memory()
        worst = _worst(worst, checks["memory"]["status"])

        # ---- CPU ----
        checks["cpu"] = await self._check_cpu()
        worst = _worst(worst, checks["cpu"]["status"])

        # ---- Disk ----
        checks["disk"] = self._check_disk()
        worst = _worst(worst, checks["disk"]["status"])

        # ---- Playback stall ----
        checks["playback"] = self._check_playback_stall()
        worst = _worst(worst, checks["playback"]["status"])

        # ---- Renderer disconnect ----
        checks["renderers"] = self._check_renderers()
        worst = _worst(worst, checks["renderers"]["status"])

        # ---- Error spike ----
        checks["errors"] = self._check_error_spike()
        worst = _worst(worst, checks["errors"]["status"])

        self._last_checks = checks
        self._last_status = worst

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    async def _check_memory(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "ok",
            "value_mb": 0,
            "threshold_mb": _MEMORY_CRITICAL_MB,
        }
        if not _HAS_PSUTIL:
            return result
        try:
            proc = psutil.Process(os.getpid())
            rss_mb = proc.memory_info().rss / (1024 * 1024)
            result["value_mb"] = round(rss_mb, 1)
            if rss_mb >= _MEMORY_CRITICAL_MB:
                result["status"] = "critical"
                self._add_alert(
                    "critical", "memory",
                    f"Utilisation memoire critique : {round(rss_mb)}MB (seuil : {_MEMORY_CRITICAL_MB}MB)",
                )
            elif rss_mb >= _MEMORY_WARNING_MB:
                result["status"] = "warning"
                self._add_alert(
                    "warning", "memory",
                    f"Utilisation memoire elevee : {round(rss_mb)}MB (seuil : {_MEMORY_WARNING_MB}MB)",
                )
        except Exception:
            logger.debug("health_memory_check_failed", exc_info=True)
        return result

    async def _check_cpu(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "ok", "value_percent": 0}
        if not _HAS_PSUTIL:
            return result
        try:
            # cpu_percent with interval runs in a blocking way, so offload
            cpu = await asyncio.get_event_loop().run_in_executor(
                None, lambda: psutil.cpu_percent(interval=5),
            )
            result["value_percent"] = round(cpu, 1)
            if cpu > 80:
                result["status"] = "warning"
                self._add_alert(
                    "warning", "cpu",
                    f"Charge CPU elevee : {round(cpu)}% (seuil : 80%)",
                )
        except Exception:
            logger.debug("health_cpu_check_failed", exc_info=True)
        return result

    def _check_disk(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "ok", "free_gb": 0}
        try:
            import shutil
            db_path = getattr(settings, "db_path", "tune_server.db")
            from pathlib import Path
            stat = shutil.disk_usage(Path(db_path).parent)
            free_gb = stat.free / (1024 ** 3)
            result["free_gb"] = round(free_gb, 1)
            if free_gb < _DISK_CRITICAL_GB:
                result["status"] = "critical"
                self._add_alert(
                    "critical", "disk",
                    f"Espace disque critique : {round(free_gb, 1)}Go restants (seuil : {_DISK_CRITICAL_GB}Go)",
                )
            elif free_gb < _DISK_WARNING_GB:
                result["status"] = "warning"
                self._add_alert(
                    "warning", "disk",
                    f"Espace disque faible : {round(free_gb, 1)}Go restants (seuil : {_DISK_WARNING_GB}Go)",
                )
        except Exception:
            logger.debug("health_disk_check_failed", exc_info=True)
        return result

    def _check_playback_stall(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "ok", "stalled_zones": []}
        try:
            from tune_server.api.deps import deps
            zm = deps.zone_manager
            if not zm:
                return result

            now = time.monotonic()
            stalled: list[str] = []

            for zone in zm.list_zones():
                try:
                    state = zone.player.state
                    pos = zone.player.position_ms
                except Exception:
                    continue

                if state.value == "playing":
                    prev = self._zone_positions.get(zone.zone_id)
                    if prev is not None:
                        prev_pos, prev_time = prev
                        elapsed = now - prev_time
                        if elapsed >= _STALL_THRESHOLD_S and pos == prev_pos:
                            stalled.append(zone.name)
                    self._zone_positions[zone.zone_id] = (pos, now)
                else:
                    # Not playing — reset tracking
                    self._zone_positions.pop(zone.zone_id, None)

            if stalled:
                result["status"] = "warning"
                result["stalled_zones"] = stalled
                self._add_alert(
                    "warning", "playback",
                    f"Lecture bloquee sur : {', '.join(stalled)}",
                )
        except Exception:
            logger.debug("health_playback_check_failed", exc_info=True)
        return result

    def _check_renderers(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "ok", "disconnected": []}
        try:
            from tune_server.api.deps import deps
            zm = deps.zone_manager
            if not zm:
                return result

            disconnected: list[str] = []
            for zone in zm.list_zones():
                try:
                    output = zone._output
                    if output and not output.is_available:
                        disconnected.append(zone.name)
                except Exception:
                    continue

            if disconnected:
                result["status"] = "warning"
                result["disconnected"] = disconnected
                self._add_alert(
                    "warning", "renderers",
                    f"Renderer deconnecte : {', '.join(disconnected)}",
                )
        except Exception:
            logger.debug("health_renderer_check_failed", exc_info=True)
        return result

    def _check_error_spike(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "ok", "count_5min": 0}
        try:
            from tune_server.utils.error_buffer import recent_errors
            errors = recent_errors()
            if not errors:
                return result

            cutoff = time.time() - _ERROR_SPIKE_WINDOW
            recent_count = 0
            for err in errors:
                try:
                    ts_str = err.get("ts", "")
                    ts = datetime.fromisoformat(ts_str).timestamp()
                    if ts >= cutoff:
                        recent_count += 1
                except Exception:
                    continue

            result["count_5min"] = recent_count
            if recent_count > _ERROR_SPIKE_THRESHOLD:
                result["status"] = "warning"
                self._add_alert(
                    "warning", "errors",
                    f"Pic d'erreurs : {recent_count} erreurs en 5 minutes (seuil : {_ERROR_SPIKE_THRESHOLD})",
                )
        except Exception:
            logger.debug("health_error_spike_check_failed", exc_info=True)
        return result

    # ------------------------------------------------------------------
    # Alert management
    # ------------------------------------------------------------------

    def _add_alert(self, level: str, category: str, message: str) -> None:
        alert = {
            "timestamp": _now_iso(),
            "level": level,
            "category": category,
            "message": message,
        }
        self._alerts.append(alert)

        # Push to WebSocket clients via EventBus
        self._event_bus.emit_nowait(Event(
            type="system.health_alert",
            data=alert,
            source="health_monitor",
        ))

        log_method = logger.warning if level == "warning" else logger.critical if level == "critical" else logger.info
        log_method("health_alert", level=level, category=category, message=message)


def _worst(current: str, new: str) -> str:
    """Return the worst status between two."""
    order = {"ok": 0, "warning": 1, "critical": 2}
    if order.get(new, 0) > order.get(current, 0):
        return new
    return current

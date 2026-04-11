from __future__ import annotations

import asyncio
import time

import structlog

from tune_server.config import settings
from tune_server.models import PlaybackState
from tune_server.zones.group import GroupManager, ZoneGroup
from tune_server.zones.zone import ZoneInstance

logger = structlog.get_logger()


class SyncEngine:
    """Monitors zone groups and corrects playback drift.

    Targets <50ms sync accuracy using:
    - High-frequency polling (100ms when playing)
    - Progressive correction (fine <50ms vs coarse >200ms)
    - Muted zone skipping
    - Latency-aware offset calibration
    """

    # <50ms sync thresholds
    DRIFT_FINE_MS = 50       # Fine correction threshold
    DRIFT_COARSE_MS = 200    # Coarse correction (full seek)
    POLL_PLAYING_S = 0.1     # 100ms polling when playing (10Hz)
    POLL_IDLE_S = 5.0        # 5s when idle
    COOLDOWN_FINE_S = 1.0    # Between fine corrections
    COOLDOWN_COARSE_S = 3.0  # Between coarse corrections

    def __init__(self, group_manager: GroupManager) -> None:
        self._group_manager = group_manager
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_correction: dict[int, float] = {}  # zone_id -> monotonic timestamp
        self._drift_history: dict[int, list[int]] = {}  # zone_id -> recent drift values

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._sync_loop())
        logger.info("sync_engine_started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _has_active_groups(self) -> bool:
        """Return True if at least one group has its leader in PLAYING state."""
        for group in self._group_manager.list_groups():
            if group.leader.player.state == PlaybackState.PLAYING:
                return True
        return False

    async def _get_zone_position_ms(self, zone: ZoneInstance) -> int:
        """Get zone position, preferring the output's hardware query."""
        try:
            pos = await zone.output.get_position_ms()
            if pos >= 0:
                return pos
        except Exception:
            pass
        # Fallback to software clock
        return zone.position_ms

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _sync_loop(self) -> None:
        while self._running:
            try:
                active = self._has_active_groups()
                interval = self.POLL_PLAYING_S if active else self.POLL_IDLE_S

                for group in self._group_manager.list_groups():
                    await self._sync_group(group)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sync_loop_error")
                interval = self.POLL_IDLE_S

            await asyncio.sleep(interval)

    async def _sync_group(self, group: ZoneGroup) -> None:
        leader = group.leader
        if leader.player.state != PlaybackState.PLAYING:
            return

        leader_pos = await self._get_zone_position_ms(leader)
        if leader_pos <= 0:
            return

        now = time.monotonic()

        # Settle period after group play start
        if hasattr(group, '_last_play_time') and group._last_play_time > 0:
            if (now - group._last_play_time) < self.COOLDOWN_COARSE_S:
                return

        for follower in group.followers:
            if follower.player.state != PlaybackState.PLAYING:
                continue

            # Skip muted zones (no need to sync audio that's silent)
            if getattr(follower, 'muted', False):
                continue

            follower_pos = await self._get_zone_position_ms(follower)
            if follower_pos <= 0:
                continue

            # Apply per-zone sync offset (from latency calibration)
            target_pos = leader_pos + follower.sync_delay_ms
            target_pos = max(0, target_pos)

            drift = abs(target_pos - follower_pos)

            # Track drift history for diagnostics
            hist = self._drift_history.setdefault(follower.zone_id, [])
            hist.append(drift)
            if len(hist) > 50:
                hist.pop(0)

            # Progressive correction
            last = self._last_correction.get(follower.zone_id, 0)

            if drift > self.DRIFT_COARSE_MS:
                # Coarse correction: large drift, full seek
                if (now - last) < self.COOLDOWN_COARSE_S:
                    continue
                logger.info(
                    "sync_coarse_correction",
                    group=group.group_id,
                    follower=follower.zone_id,
                    drift_ms=drift,
                    target_pos=target_pos,
                )
                try:
                    await follower.player.seek(target_pos)
                    self._last_correction[follower.zone_id] = now
                except Exception:
                    logger.exception("sync_seek_error", follower=follower.zone_id)

            elif drift > self.DRIFT_FINE_MS:
                # Fine correction: small drift, micro-seek
                if (now - last) < self.COOLDOWN_FINE_S:
                    continue
                logger.debug(
                    "sync_fine_correction",
                    follower=follower.zone_id,
                    drift_ms=drift,
                )
                try:
                    await follower.player.seek(target_pos)
                    self._last_correction[follower.zone_id] = now
                except Exception:
                    pass

    def get_drift_stats(self) -> dict[int, dict]:
        """Get drift statistics per zone for health monitoring."""
        stats = {}
        for zone_id, hist in self._drift_history.items():
            if not hist:
                continue
            stats[zone_id] = {
                "current_drift_ms": hist[-1],
                "avg_drift_ms": round(sum(hist) / len(hist)),
                "max_drift_ms": max(hist),
                "samples": len(hist),
                "in_sync": hist[-1] < self.DRIFT_FINE_MS,
            }
        return stats

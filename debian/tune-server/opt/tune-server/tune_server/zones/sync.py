from __future__ import annotations

import asyncio
import time

import structlog

from tune_server.models import PlaybackState
from tune_server.zones.group import GroupManager, ZoneGroup

logger = structlog.get_logger()

# Only correct if drift exceeds this threshold
DRIFT_THRESHOLD_MS = 1000
# Minimum time between corrections for a given follower
CORRECTION_COOLDOWN_S = 30.0
# How often to check
SYNC_POLL_INTERVAL_S = 5.0


class SyncEngine:
    """Monitors zone groups and corrects playback drift."""

    def __init__(self, group_manager: GroupManager) -> None:
        self._group_manager = group_manager
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_correction: dict[int, float] = {}  # zone_id -> timestamp

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

    async def _sync_loop(self) -> None:
        while self._running:
            try:
                for group in self._group_manager.list_groups():
                    await self._sync_group(group)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sync_loop_error")

            await asyncio.sleep(SYNC_POLL_INTERVAL_S)

    async def _sync_group(self, group: ZoneGroup) -> None:
        leader = group.leader
        leader_pos = leader.position_ms

        if leader_pos <= 0:
            return

        if leader.player.state != PlaybackState.PLAYING:
            return

        now = time.monotonic()

        # Don't correct right after a group play (let the staggered start settle)
        if hasattr(group, '_last_play_time') and group._last_play_time > 0:
            if (now - group._last_play_time) < CORRECTION_COOLDOWN_S:
                return

        for follower in group.followers:
            if follower.player.state != PlaybackState.PLAYING:
                continue

            follower_pos = follower.position_ms
            if follower_pos <= 0:
                continue

            drift = abs(leader_pos - follower_pos)

            if drift > DRIFT_THRESHOLD_MS:
                # Check cooldown
                last = self._last_correction.get(follower.zone_id, 0)
                if (now - last) < CORRECTION_COOLDOWN_S:
                    continue

                logger.info(
                    "sync_correction",
                    group=group.group_id,
                    leader=leader.zone_id,
                    follower=follower.zone_id,
                    drift_ms=drift,
                )
                try:
                    await follower.player.seek(leader_pos)
                    self._last_correction[follower.zone_id] = now
                except Exception:
                    logger.exception(
                        "sync_seek_error",
                        follower=follower.zone_id,
                    )

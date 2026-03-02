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

    Uses configurable settings for polling interval, drift threshold,
    and correction cooldown.  Queries output positions when available
    (DLNA GetPositionInfo, AirPlay metadata, local elapsed time) and
    falls back to the zone's software clock.
    """

    def __init__(self, group_manager: GroupManager) -> None:
        self._group_manager = group_manager
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_correction: dict[int, float] = {}  # zone_id -> monotonic timestamp

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
                interval = (
                    settings.sync_poll_playing_interval
                    if active
                    else settings.sync_poll_idle_interval
                )

                for group in self._group_manager.list_groups():
                    await self._sync_group(group)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sync_loop_error")
                interval = settings.sync_poll_idle_interval

            await asyncio.sleep(interval)

    async def _sync_group(self, group: ZoneGroup) -> None:
        leader = group.leader
        if leader.player.state != PlaybackState.PLAYING:
            return

        leader_pos = await self._get_zone_position_ms(leader)
        if leader_pos <= 0:
            return

        now = time.monotonic()

        # Don't correct right after a group play (let the staggered start settle)
        if hasattr(group, '_last_play_time') and group._last_play_time > 0:
            if (now - group._last_play_time) < settings.sync_correction_cooldown_s:
                return

        for follower in group.followers:
            if follower.player.state != PlaybackState.PLAYING:
                continue

            follower_pos = await self._get_zone_position_ms(follower)
            if follower_pos <= 0:
                continue

            # Apply per-zone sync offset
            target_pos = leader_pos + follower.sync_delay_ms
            target_pos = max(0, target_pos)

            drift = abs(target_pos - follower_pos)

            if drift > settings.sync_drift_threshold_ms:
                # Check cooldown
                last = self._last_correction.get(follower.zone_id, 0)
                if (now - last) < settings.sync_correction_cooldown_s:
                    continue

                logger.info(
                    "sync_correction",
                    group=group.group_id,
                    leader=leader.zone_id,
                    follower=follower.zone_id,
                    drift_ms=drift,
                    target_pos=target_pos,
                    offset_ms=follower.sync_delay_ms,
                )
                try:
                    await follower.player.seek(target_pos)
                    self._last_correction[follower.zone_id] = now
                except Exception:
                    logger.exception(
                        "sync_seek_error",
                        follower=follower.zone_id,
                    )

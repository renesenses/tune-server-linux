from __future__ import annotations

import asyncio
import uuid

import structlog

from tune_server.config import settings
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import OutputType, PlaybackState, Track
from tune_server.zones.zone import ZoneInstance

logger = structlog.get_logger()

# Module-level cache: device_name -> measured latency in seconds
_dlna_latency_cache: dict[str, float] = {}


class ZoneGroup:
    """A group of zones that play in sync. One zone is the leader."""

    def __init__(
        self,
        group_id: str,
        leader: ZoneInstance,
        followers: list[ZoneInstance],
        event_bus: EventBus,
    ) -> None:
        self._group_id = group_id
        self._leader = leader
        self._followers = list(followers)
        self._event_bus = event_bus
        self._last_play_time: float = 0

    @property
    def group_id(self) -> str:
        return self._group_id

    @property
    def leader(self) -> ZoneInstance:
        return self._leader

    @property
    def followers(self) -> list[ZoneInstance]:
        return list(self._followers)

    @property
    def all_zones(self) -> list[ZoneInstance]:
        return [self._leader] + self._followers

    @property
    def zone_ids(self) -> list[int]:
        return [z.zone_id for z in self.all_zones]

    async def play(self, tracks: list[Track], start_position: int = 0) -> None:
        """Play on all zones in the group, waiting for DLNA to actually start."""
        network_zones = [z for z in self.all_zones if z.output_type in (OutputType.DLNA, OutputType.AIRPLAY)]
        local_zones = [z for z in self.all_zones if z not in network_zones]

        if not network_zones:
            for zone in self.all_zones:
                await zone.player.play(tracks=tracks, start_position=start_position)
            self._last_play_time = asyncio.get_running_loop().time()
            return

        # Start network outputs first
        for zone in network_zones:
            await zone.player.play(tracks=tracks, start_position=start_position)

        # Wait for the DLNA renderer to actually connect to our HTTP stream
        dlna_output = network_zones[0].output
        session = dlna_output.get_current_session() if hasattr(dlna_output, 'get_current_session') else None
        if session and hasattr(session, 'client_connected'):
            try:
                await asyncio.wait_for(session.client_connected.wait(), timeout=5.0)
                logger.info("dlna_renderer_connected")

                # Use cached latency if available, otherwise default buffer
                device_name = dlna_output.name
                cached_latency = _dlna_latency_cache.get(device_name)
                if cached_latency is not None:
                    buffer_s = cached_latency
                    logger.info("dlna_using_cached_latency", device=device_name, latency_s=round(buffer_s, 2))
                else:
                    buffer_s = settings.sync_dlna_default_buffer_s
                    # Fire-and-forget: measure actual latency for next time
                    if hasattr(dlna_output, 'measure_latency'):
                        asyncio.create_task(
                            self._measure_and_cache_latency(dlna_output)
                        )

                await asyncio.sleep(buffer_s)
            except asyncio.TimeoutError:
                logger.warning("dlna_connect_timeout_fallback")
        else:
            await asyncio.sleep(2.0)

        # Start local outputs
        for zone in local_zones:
            await zone.player.play(tracks=tracks, start_position=start_position)

        self._last_play_time = asyncio.get_running_loop().time()

    @staticmethod
    async def _measure_and_cache_latency(dlna_output) -> None:
        """Background task: measure DLNA latency and cache it."""
        try:
            latency = await dlna_output.measure_latency()
            if latency is not None:
                _dlna_latency_cache[dlna_output.name] = latency
                logger.info("dlna_latency_cached", device=dlna_output.name, latency_s=round(latency, 2))
        except Exception:
            logger.debug("dlna_latency_measure_failed", device=dlna_output.name)

    async def pause(self) -> None:
        for zone in self.all_zones:
            await zone.player.pause()

    async def resume(self) -> None:
        for zone in self.all_zones:
            await zone.player.resume()

    async def stop(self) -> None:
        for zone in self.all_zones:
            await zone.player.stop()

    async def skip_next(self) -> None:
        for zone in self.all_zones:
            await zone.player.skip_next()

    async def skip_previous(self) -> None:
        for zone in self.all_zones:
            await zone.player.skip_previous()

    def add_follower(self, zone: ZoneInstance) -> None:
        if zone not in self._followers and zone != self._leader:
            self._followers.append(zone)
            zone.group_id = self._group_id

    def remove_follower(self, zone: ZoneInstance) -> None:
        if zone in self._followers:
            self._followers.remove(zone)
            zone.group_id = None


class GroupManager:
    """Manages zone groups for multi-room playback."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._groups: dict[str, ZoneGroup] = {}

    def get_group(self, group_id: str) -> ZoneGroup | None:
        return self._groups.get(group_id)

    def get_group_for_zone(self, zone_id: int) -> ZoneGroup | None:
        for group in self._groups.values():
            if zone_id in group.zone_ids:
                return group
        return None

    async def create_group(
        self,
        leader: ZoneInstance,
        followers: list[ZoneInstance],
    ) -> ZoneGroup:
        group_id = str(uuid.uuid4())[:8]

        group = ZoneGroup(group_id, leader, followers, self._event_bus)

        # Tag all zones
        leader.group_id = group_id
        for f in followers:
            f.group_id = group_id

        self._groups[group_id] = group

        # If leader is playing, sync followers to the same track + position
        if leader.player.state == PlaybackState.PLAYING and leader.player.current_track:
            track = leader.player.current_track
            leader_pos = leader.position_ms
            for f in followers:
                try:
                    # Set the track in the queue, then start at leader's position
                    f.player.queue.set_tracks([track])
                    await f.player._start_track(track, seek_ms=leader_pos)
                except Exception:
                    logger.exception("group_sync_follower_error", zone_id=f.zone_id)

        await self._event_bus.emit(Event(
            type=EventType.ZONE_GROUPED,
            data={
                "group_id": group_id,
                "leader_id": leader.zone_id,
                "zone_ids": group.zone_ids,
            },
            source="group_manager",
        ))

        logger.info("zone_group_created", group_id=group_id, zones=group.zone_ids)
        return group

    async def dissolve_group(self, group_id: str) -> None:
        group = self._groups.pop(group_id, None)
        if not group:
            return

        for zone in group.all_zones:
            zone.group_id = None

        await self._event_bus.emit(Event(
            type=EventType.ZONE_UNGROUPED,
            data={"group_id": group_id},
            source="group_manager",
        ))

        logger.info("zone_group_dissolved", group_id=group_id)

    def list_groups(self) -> list[ZoneGroup]:
        return list(self._groups.values())

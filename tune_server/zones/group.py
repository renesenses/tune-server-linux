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
            try:
                await zone.player.pause()
            except Exception:
                logger.exception("group_pause_error", zone_id=zone.zone_id)

    async def resume(self) -> None:
        for zone in self.all_zones:
            try:
                await zone.player.resume()
            except Exception:
                logger.exception("group_resume_error", zone_id=zone.zone_id)

    async def stop(self) -> None:
        for zone in self.all_zones:
            try:
                await zone.player.stop()
            except Exception:
                logger.exception("group_stop_error", zone_id=zone.zone_id)

    async def skip_next(self) -> None:
        for zone in self.all_zones:
            try:
                await zone.player.skip_next()
            except Exception:
                logger.exception("group_skip_next_error", zone_id=zone.zone_id)

    async def skip_previous(self) -> None:
        for zone in self.all_zones:
            try:
                await zone.player.skip_previous()
            except Exception:
                logger.exception("group_skip_previous_error", zone_id=zone.zone_id)

    def add_follower(self, zone: ZoneInstance) -> None:
        if zone not in self._followers and zone != self._leader:
            self._followers.append(zone)
            zone.group_id = self._group_id

    def remove_follower(self, zone: ZoneInstance) -> None:
        if zone in self._followers:
            self._followers.remove(zone)
            zone.group_id = None


class GroupManager:
    """Manages zone groups for multi-room playback.

    Since v0.8.0 the manager also owns a CompositeGroup per ZoneGroup,
    which dispatches sync to the right native technology (SoCo for
    Sonos, snapserver JSON-RPC for Snapcast) instead of the legacy
    per-zone `sync_delay_ms` calibration loop. The SnapcastManager and
    SonosManager are wired in via `set_runtime_managers()` after the
    GroupManager is constructed (to avoid a circular import — they
    both depend on the event bus that the GroupManager already holds).
    """

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._groups: dict[str, ZoneGroup] = {}
        # Filled by app.py once SnapcastManager / SonosManager exist.
        # Kept loosely typed (Optional[Any]) so importing this module
        # never drags soco / snapserver-related deps in.
        self._snapcast_manager = None
        self._sonos_manager = None
        self._composite_groups: dict[str, "CompositeGroup"] = {}  # type: ignore[name-defined]

    def set_runtime_managers(self, snapcast_manager=None, sonos_manager=None) -> None:
        """Inject the typed-tech managers after construction. Safe to
        call multiple times — subsequent calls overwrite."""
        self._snapcast_manager = snapcast_manager
        self._sonos_manager = sonos_manager

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

        # v0.8.0: build a CompositeGroup view of this ZoneGroup. Active
        # native sync where the technology supports it (SoCo, Snapcast).
        # Does nothing for purely LOCAL/DLNA/AIRPLAY groups — those keep
        # the legacy ZoneGroup.play() path with DLNA latency-buffer
        # alignment. Failures are non-fatal — composite is additive,
        # the existing best-effort sync is the fallback.
        try:
            await self._activate_composite(group)
        except Exception:
            logger.exception("composite_group_activation_failed", group_id=group_id)

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

        # Tear down the composite first so native groups (SoCo / snapcast)
        # un-link before we clear zone.group_id and lose track of who was
        # in it.
        try:
            await self._dissolve_composite(group_id, group)
        except Exception:
            logger.exception("composite_group_dissolution_failed", group_id=group_id)

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

    # --- v0.8.0 composite-group wiring ---------------------------------

    async def _activate_composite(self, group: "ZoneGroup") -> None:
        """Build a CompositeGroup view of the ZoneGroup and trigger
        native sync joins (SoCo / snapcast). Pure no-op when neither
        manager is configured or when the group contains no
        Snapcast/Sonos zones — the legacy DLNA/AirPlay path is
        preserved unchanged."""
        if self._snapcast_manager is None and self._sonos_manager is None:
            return
        zones = group.all_zones
        techs = {z.output_type for z in zones}
        if OutputType.SNAPCAST not in techs and OutputType.SONOS not in techs:
            return

        from tune_server.zones.composite_group import CompositeGroup
        # Project ZoneInstances into _ZoneView so build_typed_groups
        # finds the `id` attribute (ZoneInstance exposes `zone_id`,
        # not `id`).
        zone_views = [self._zone_view(z) for z in zones]
        composite = CompositeGroup.from_zones(group.group_id, zone_views)
        manager_bag = {
            "snapcast": self._snapcast_manager,
            "sonos": self._sonos_manager,
        }
        zones_by_id = {v.id: v for v in zone_views}
        # join() is the native group activation; we DON'T compute
        # per-tech start offsets here — that's the player-level
        # concern when playback actually starts. CompositeGroup's
        # `start_playback()` will be invoked from the player loop in
        # a follow-up patch (v0.8.1 once Matteo validates the join
        # path).
        for typed in composite.typed_groups:
            await typed.join(manager_bag, zones_by_id)
        self._composite_groups[group.group_id] = composite
        logger.info(
            "composite_group_activated",
            group_id=group.group_id,
            techs=[t.value for t in composite.technologies],
        )

    async def _dissolve_composite(
        self, group_id: str, group: "ZoneGroup",
    ) -> None:
        composite = self._composite_groups.pop(group_id, None)
        if composite is None:
            return
        manager_bag = {
            "snapcast": self._snapcast_manager,
            "sonos": self._sonos_manager,
        }
        zone_views = [self._zone_view(z) for z in group.all_zones]
        zones_by_id = {v.id: v for v in zone_views}
        await composite.dissolve(manager_bag, zones_by_id)

    @staticmethod
    def _zone_view(zone_instance) -> "_ZoneView":
        """Adapt a ZoneInstance into the duck-typed view that
        TypedGroup methods expect (id, output_type,
        output_device_id, snapcast_stream_name, snapcast_client_ids)."""
        return _ZoneView(
            id=zone_instance.zone_id,
            output_type=zone_instance.output_type,
            output_device_id=getattr(zone_instance, "output_device_id", None),
            snapcast_stream_name=getattr(
                zone_instance, "snapcast_stream_name", None,
            ),
            snapcast_client_ids=getattr(
                zone_instance, "snapcast_client_ids", []
            ) or [],
        )


from dataclasses import dataclass as _dataclass  # noqa: E402


@_dataclass
class _ZoneView:
    """Lightweight projection of a ZoneInstance with the fields the
    TypedGroup classes need. Avoids tying typed_groups.py to ZoneInstance
    so the multi-room logic stays unit-testable in isolation."""

    id: int
    output_type: OutputType
    output_device_id: str | None = None
    snapcast_stream_name: str | None = None
    snapcast_client_ids: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.snapcast_client_ids is None:
            self.snapcast_client_ids = []

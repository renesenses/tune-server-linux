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
        # v0.8.0 multi-room: composite + delay_repo set by GroupManager
        # via `set_composite(...)` after the typed-group join. play()
        # uses them to honor per-tech start offsets when the group
        # mixes Snapcast/Sonos with other techs.
        self._composite = None  # CompositeGroup | None
        self._delay_repo = None  # GroupDelayRepo | None
        self._manager_bag: dict = {}

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

    def set_composite(self, composite, delay_repo, manager_bag: dict) -> None:
        """Attach the v0.8.0 CompositeGroup + GroupDelayRepo to this
        group so play() can honor per-tech start offsets. Called by
        GroupManager._activate_composite once the typed groups are
        joined natively."""
        self._composite = composite
        self._delay_repo = delay_repo
        self._manager_bag = manager_bag or {}

    async def play(self, tracks: list[Track], start_position: int = 0) -> None:
        """Play on all zones in the group.

        Three regimes since v0.8.0:
          1. Mixed-techno composite group (Snapcast + Sonos + maybe
             others): use CompositeGroup.start_playback() to compute
             per-technology start offsets and schedule each tech's
             zones with the right delay.
          2. Network zones present (DLNA/AirPlay) but no composite:
             keep the legacy "wait for renderer connection + cached
             latency buffer" path.
          3. All-local: kick everyone off immediately.
        """
        # Path 1: composite-aware multi-tech start.
        if self._composite is not None and not self._composite.is_homogeneous:
            await self._play_with_composite_offsets(tracks, start_position)
            return

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

    async def _play_with_composite_offsets(
        self, tracks: list[Track], start_position: int = 0,
    ) -> None:
        """Mixed-techno start path. Asks CompositeGroup for the
        per-technology start offsets (calibrated via group_delays in
        DB), then walks technologies in offset order — earliest first,
        each subsequent tech sleeping the *delta* from the previous
        offset (not the absolute, otherwise sleeps would stack
        cumulatively).

        Concrete example: two pairs are calibrated such that
        Snapcast=0 ms and Sonos=120 ms. We start the Snapcast bucket
        immediately, sleep 120 ms, then start the Sonos bucket. Sonos
        speakers come out 120 ms after the Snapcast clients — which
        is exactly the offset the user calibrated to compensate for
        Sonos's slower buffer fill.
        """
        if self._composite is None or self._delay_repo is None:
            # Defensive — set_composite() should have populated both.
            for zone in self.all_zones:
                await zone.player.play(tracks=tracks, start_position=start_position)
            self._last_play_time = asyncio.get_running_loop().time()
            return

        zone_views = [
            GroupManager._zone_view(z) for z in self.all_zones
        ]
        zones_by_id = {v.id: v for v in zone_views}
        try:
            offsets = await self._composite.start_playback(
                manager_bag=self._manager_bag,
                zones_by_id=zones_by_id,
                delay_repo=self._delay_repo,
            )
        except Exception:
            logger.exception(
                "composite_start_playback_failed_falling_back",
                group_id=self._group_id,
            )
            for zone in self.all_zones:
                await zone.player.play(tracks=tracks, start_position=start_position)
            self._last_play_time = asyncio.get_running_loop().time()
            return

        # Bucket zones by output_type, then walk in ascending offset.
        zones_by_tech: dict = {}
        for zone in self.all_zones:
            zones_by_tech.setdefault(zone.output_type, []).append(zone)

        ordered = sorted(offsets.items(), key=lambda kv: kv[1])
        previous_offset_ms = 0
        for tech, offset_ms in ordered:
            delta_ms = max(0, offset_ms - previous_offset_ms)
            if delta_ms > 0:
                await asyncio.sleep(delta_ms / 1000.0)
            for zone in zones_by_tech.get(tech, []):
                try:
                    await zone.player.play(
                        tracks=tracks, start_position=start_position,
                    )
                except Exception:
                    logger.exception(
                        "composite_tech_play_failed",
                        zone_id=zone.zone_id, tech=tech.value,
                    )
            previous_offset_ms = offset_ms
        self._last_play_time = asyncio.get_running_loop().time()
        logger.info(
            "composite_play_started",
            group_id=self._group_id,
            offsets={t.value: ms for t, ms in offsets.items()},
        )

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
        self._db = None  # populated by set_runtime_managers; needed
        # for GroupDelayRepo lookups during composite play().
        self._composite_groups: dict[str, "CompositeGroup"] = {}  # type: ignore[name-defined]

    def set_runtime_managers(
        self, snapcast_manager=None, sonos_manager=None, db=None,
    ) -> None:
        """Inject the typed-tech managers (and the DB handle for
        GroupDelayRepo) after construction. Safe to call multiple
        times — subsequent calls overwrite."""
        self._snapcast_manager = snapcast_manager
        self._sonos_manager = sonos_manager
        self._db = db

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
        # Attach to the ZoneGroup so play() can call
        # CompositeGroup.start_playback() with the right delay_repo.
        delay_repo = None
        if self._db is not None:
            from tune_server.zones.composite_group import GroupDelayRepo
            delay_repo = GroupDelayRepo(self._db)
        group.set_composite(composite, delay_repo, manager_bag)
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

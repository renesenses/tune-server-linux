"""Typed group abstractions for v0.8.0 multi-room.

The architecture (per Matteo's design):

  ZoneGroup (legacy, kept for backward compat through v0.8.x)
       ↓ refactor into ↓

  SonosGroup    → driven by SoCo, native sample-accurate sync inside
                  the Sonos household.
  SnapcastGroup → driven by snapserver, ~50 ms native sync inside the
                  Snapcast cohort.
  LocalGroup    → degenerate single zone (sounddevice / DLNA / AirPlay
                  one-shot). No sync needed by definition.

  CompositeGroup → composes 1+ typed groups + applies ONE
                   `group_delay_ms` per pair of technologies, calibrated
                   ONCE and stored in DB. Replaces the per-zone
                   `sync_delay_ms` recalibration that lived in
                   zones/sync.py + zones/latency.py.

This module ships the typed-group classes only. CompositeGroup +
the calibration REST flow live in `composite_group.py` next door.
The legacy `GroupManager` in `group.py` still works — composite
support is added alongside, not replacing it. Removal of legacy
sync code = v0.9.0.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Optional

import structlog

from tune_server.models import OutputType

if TYPE_CHECKING:
    from tune_server.zones.snapcast_manager import SnapcastManager
    from tune_server.zones.sonos_manager import SonosManager
    from tune_server.zones.zone import Zone

logger = structlog.get_logger()


@dataclass
class TypedGroup(ABC):
    """A homogeneous group: all members share one OutputType and one
    native sync mechanism. Cross-technology coordination is the
    CompositeGroup's responsibility."""

    name: str
    members: list[int] = field(default_factory=list)  # zone IDs

    @property
    @abstractmethod
    def technology(self) -> OutputType:
        """Identifies the group's native sync domain."""

    @abstractmethod
    async def join(self, manager_bag: dict, zones_by_id: dict[int, "Zone"]) -> None:
        """Commit the group to the underlying technology — call SoCo's
        join() / snapcast Group.SetClients / etc. Idempotent."""

    @abstractmethod
    async def dissolve(self, manager_bag: dict, zones_by_id: dict[int, "Zone"]) -> None:
        """Tear the group down. Members revert to standalone."""

    def add_zone(self, zone_id: int) -> None:
        if zone_id not in self.members:
            self.members.append(zone_id)

    def remove_zone(self, zone_id: int) -> None:
        if zone_id in self.members:
            self.members.remove(zone_id)


@dataclass
class SonosGroup(TypedGroup):
    """Native Sonos S2 group. Sync is sample-accurate by construction —
    no per-track recalibration ever needed."""

    @property
    def technology(self) -> OutputType:
        return OutputType.SONOS

    async def join(self, manager_bag: dict, zones_by_id: dict[int, "Zone"]) -> None:
        sonos: Optional["SonosManager"] = manager_bag.get("sonos")
        if sonos is None or not self.members:
            return
        # Use the first member as coordinator. Persist the choice
        # would belong on the GroupManager / DB; for now first-wins.
        coord_zone = zones_by_id.get(self.members[0])
        if coord_zone is None:
            return
        coord_uid = self._zone_uid(coord_zone)
        member_uids = [
            self._zone_uid(zones_by_id[z])
            for z in self.members
            if z in zones_by_id
        ]
        member_uids = [u for u in member_uids if u]
        if not coord_uid or not member_uids:
            return
        await sonos.set_group(coord_uid, member_uids)

    async def dissolve(self, manager_bag: dict, zones_by_id: dict[int, "Zone"]) -> None:
        sonos: Optional["SonosManager"] = manager_bag.get("sonos")
        if sonos is None:
            return
        for zid in self.members:
            zone = zones_by_id.get(zid)
            if zone is None:
                continue
            uid = self._zone_uid(zone)
            if uid:
                await sonos.unjoin(uid)

    @staticmethod
    def _zone_uid(zone) -> str | None:
        # Sonos zones store the speaker RINCON UID in `output_device_id`.
        return getattr(zone, "output_device_id", None)


@dataclass
class SnapcastGroup(TypedGroup):
    """Native Snapcast group. ~50 ms sync across all subscribed clients
    — the snapserver buffers + timestamps the stream so all clients
    play the same sample at the same wall-clock instant."""

    @property
    def technology(self) -> OutputType:
        return OutputType.SNAPCAST

    async def join(self, manager_bag: dict, zones_by_id: dict[int, "Zone"]) -> None:
        snap: Optional["SnapcastManager"] = manager_bag.get("snapcast")
        if snap is None or not self.members:
            return
        # All members share the leader zone's snapcast stream so the
        # snapserver groups every client onto the one stream.
        leader = zones_by_id.get(self.members[0])
        if leader is None:
            return
        leader_stream = getattr(leader, "snapcast_stream_name", None)
        if not leader_stream:
            return
        # Collect every client UUID across all member zones.
        client_ids: list[str] = []
        for zid in self.members:
            z = zones_by_id.get(zid)
            if z is None:
                continue
            client_ids.extend(getattr(z, "snapcast_client_ids", []) or [])
        if client_ids:
            await snap.set_clients_for_stream(leader_stream, client_ids)

    async def dissolve(self, manager_bag: dict, zones_by_id: dict[int, "Zone"]) -> None:
        snap: Optional["SnapcastManager"] = manager_bag.get("snapcast")
        if snap is None:
            return
        # Each member zone reverts to its own stream. Snapserver groups
        # are recreated by reassigning each zone's clients to its own
        # stream name.
        for zid in self.members:
            z = zones_by_id.get(zid)
            if z is None:
                continue
            stream = getattr(z, "snapcast_stream_name", None)
            client_ids = getattr(z, "snapcast_client_ids", []) or []
            if stream and client_ids:
                await snap.set_clients_for_stream(stream, client_ids)


@dataclass
class LocalGroup(TypedGroup):
    """A single-member group whose underlying output is one of the
    non-natively-syncing types (LOCAL, DLNA, AIRPLAY). Sync between
    LocalGroups is impossible at sample-accuracy and only meaningful
    inside a CompositeGroup with `group_delay_ms` calibration."""

    output_type: OutputType = OutputType.LOCAL

    @property
    def technology(self) -> OutputType:
        return self.output_type

    async def join(self, manager_bag: dict, zones_by_id: dict[int, "Zone"]) -> None:
        # No native group to join — the OutputTarget itself drives the
        # zone. CompositeGroup will set the inter-group delay before
        # playback starts.
        return None

    async def dissolve(self, manager_bag: dict, zones_by_id: dict[int, "Zone"]) -> None:
        return None


def build_typed_groups(
    zones: Iterable["Zone"],
) -> list[TypedGroup]:
    """Bucket zones by technology into TypedGroups. Used by
    CompositeGroup factory when transitioning legacy ZoneGroups into
    the new model."""
    by_tech: dict[OutputType, list[int]] = {}
    for z in zones:
        ot: OutputType = getattr(z, "output_type", OutputType.LOCAL)
        by_tech.setdefault(ot, []).append(getattr(z, "id", 0))
    out: list[TypedGroup] = []
    for ot, ids in by_tech.items():
        if not ids:
            continue
        if ot == OutputType.SONOS:
            out.append(SonosGroup(name=f"sonos-{ids[0]}", members=ids))
        elif ot == OutputType.SNAPCAST:
            out.append(SnapcastGroup(name=f"snapcast-{ids[0]}", members=ids))
        else:
            out.append(LocalGroup(
                name=f"{ot.value}-{ids[0]}",
                members=ids, output_type=ot,
            ))
    return out

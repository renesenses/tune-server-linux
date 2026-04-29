"""Sonos lifecycle + native grouping via SoCo.

This is the SonosGroup leg of the v0.8.0 multi-room refactor. SoCo
(https://github.com/SoCo/SoCo) talks UPnP/SOAP to Sonos S2 speakers
and exposes their native group join/leave primitives — sample-
accurate sync inside the Sonos household, no per-track recalibration.

We do NOT try to mix Sonos and DLNA/AirPlay/Snapcast inside a single
sync domain. Cross-techno alignment is the CompositeGroup's job and
uses ONE calibrated `group_delay_ms` per pair, applied once at
playback start.

Discovery is best-effort over UDP 1900 (SSDP M-SEARCH). soco.discover()
returns a set of ZoneGroupCoordinator-aware SoCo objects; we cache the
last result and re-poll on register/unregister. UPnP events for
group-membership changes are a v0.8.x follow-up — for now any Tune-
side action triggers a fresh discover().
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

import structlog

logger = structlog.get_logger()


@dataclass(frozen=True)
class SonosSpeaker:
    """A speaker as discovered by SoCo, with the bits Tune needs."""

    uid: str           # RINCON_xxx — stable identifier, persists across reboots
    name: str          # zone player name as configured in Sonos app
    ip: str            # current IP on the LAN
    is_coordinator: bool  # True if this speaker is the head of its native group
    group_uid: str | None  # UID of the group's coordinator (None if standalone)


class SonosManager:
    """Discovers Sonos speakers and orchestrates native group join/leave.

    Public surface:
      - `start()` / `stop()` lifecycle
      - `discover()` -> list[SonosSpeaker]
      - `get(uid)` -> Optional[SoCo speaker object]
      - `set_group(coordinator_uid, member_uids)` — joins each member
        to the coordinator's native group; lone coordinator = ungroup.
      - `play_uri(uid, uri, metadata=None)` — pushes a stream URL to
        the speaker (typically the Tune HTTP streamer URL for the zone).

    Soco is sync — wrap calls via asyncio.to_thread to keep the event
    loop free.
    """

    def __init__(self) -> None:
        self._speakers: dict[str, Any] = {}  # uid -> soco.SoCo
        self._discovery_lock = asyncio.Lock()

    @property
    def is_supported(self) -> bool:
        # Tune supports Sonos on every server platform — soco itself is
        # pure Python and runs anywhere. Discovery requires LAN
        # multicast access, which is the user's network responsibility.
        try:
            import soco  # noqa: F401
            return True
        except ImportError:
            return False

    async def start(self) -> None:
        if not self.is_supported:
            logger.debug("sonos_skip_no_soco")
            return
        await self.discover()
        logger.info(
            "sonos_manager_started", discovered=len(self._speakers),
        )

    async def stop(self) -> None:
        self._speakers.clear()

    # --- discovery ----------------------------------------------------

    async def discover(self, timeout_s: int = 5) -> list[SonosSpeaker]:
        """Run a fresh SSDP discovery and refresh the speaker cache."""
        if not self.is_supported:
            return []
        async with self._discovery_lock:
            try:
                import soco
                found = await asyncio.to_thread(soco.discover, timeout_s)
            except Exception as exc:
                logger.warning("sonos_discover_failed", error=repr(exc))
                return []
            if not found:
                self._speakers.clear()
                return []
            self._speakers = {sp.uid: sp for sp in found}
        return [self._snapshot(sp) for sp in self._speakers.values()]

    def _snapshot(self, sp: Any) -> SonosSpeaker:
        try:
            group = sp.group
            coord_uid = group.coordinator.uid if group and group.coordinator else None
            is_coord = coord_uid == sp.uid
        except Exception:
            coord_uid = None
            is_coord = False
        return SonosSpeaker(
            uid=sp.uid,
            name=getattr(sp, "player_name", sp.uid),
            ip=getattr(sp, "ip_address", ""),
            is_coordinator=is_coord,
            group_uid=coord_uid,
        )

    def get(self, uid: str) -> Any | None:
        return self._speakers.get(uid)

    async def list_speakers(self) -> list[SonosSpeaker]:
        return [self._snapshot(sp) for sp in self._speakers.values()]

    # --- grouping -----------------------------------------------------

    async def set_group(
        self, coordinator_uid: str, member_uids: list[str],
    ) -> None:
        """Join each member to the coordinator's native group. Members
        already in the right group are no-ops. SoCo enforces "one
        coordinator per group" by definition — joining means leaving
        whatever previous group the member was in."""
        coord = self.get(coordinator_uid)
        if coord is None:
            logger.warning("sonos_set_group_unknown_coordinator", uid=coordinator_uid)
            return
        for uid in member_uids:
            if uid == coordinator_uid:
                continue
            member = self.get(uid)
            if member is None:
                logger.debug("sonos_set_group_unknown_member", uid=uid)
                continue
            try:
                await asyncio.to_thread(member.join, coord)
            except Exception as exc:
                logger.warning(
                    "sonos_join_failed",
                    coordinator=coordinator_uid, member=uid, error=repr(exc),
                )

    async def unjoin(self, uid: str) -> None:
        """Detach a speaker from its current group — it becomes its
        own coordinator."""
        sp = self.get(uid)
        if sp is None:
            return
        try:
            await asyncio.to_thread(sp.unjoin)
        except Exception as exc:
            logger.warning("sonos_unjoin_failed", uid=uid, error=repr(exc))

    # --- playback -----------------------------------------------------

    async def play_uri(
        self, uid: str, uri: str, metadata: str = "",
    ) -> None:
        """Push a stream URL to the speaker (or its group coordinator).
        Group followers receive the same stream automatically — that's
        the whole point of native Sonos sync."""
        sp = self.get(uid)
        if sp is None:
            logger.warning("sonos_play_uri_unknown_speaker", uid=uid)
            return
        try:
            await asyncio.to_thread(sp.play_uri, uri, metadata)
        except Exception as exc:
            logger.warning(
                "sonos_play_uri_failed", uid=uid, uri=uri, error=repr(exc),
            )

    async def stop_playback(self, uid: str) -> None:
        sp = self.get(uid)
        if sp is None:
            return
        try:
            await asyncio.to_thread(sp.stop)
        except Exception as exc:
            logger.warning("sonos_stop_failed", uid=uid, error=repr(exc))

    async def set_volume(self, uid: str, volume: int) -> None:
        """Per-speaker volume (0–100). Use the snapcast-style
        OutputTarget volume API to call this."""
        sp = self.get(uid)
        if sp is None:
            return
        volume = max(0, min(100, int(volume)))
        try:
            await asyncio.to_thread(setattr, sp, "volume", volume)
        except Exception as exc:
            logger.warning("sonos_volume_failed", uid=uid, error=repr(exc))

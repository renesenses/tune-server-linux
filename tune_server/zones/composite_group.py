"""CompositeGroup — composes typed groups across technologies.

The architecture (Matteo's design):

    CompositeGroup
      ├─ SonosGroup     (native S2 sync)
      ├─ SnapcastGroup  (native ~50 ms sync)
      └─ LocalGroup     (single zone, no intra-group sync)
    + group_delay_ms applied ONCE at playback start, per pair of
      technologies, calibrated once and stored in DB.

This replaces the legacy `_calibrate_position_delta` / `_apply_sync_delay`
loop that recomputed offsets for every track. Cross-technology drift
is mostly speaker-buffer + network latency — both stable across tracks
on the same hardware, so a single calibration is enough.

`group_delays` (DB table) holds the canonical pairs. Lookups use
alphabetical canonicalisation so (a, b) and (b, a) hit the same row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Optional

import structlog

from tune_server.models import OutputType
from tune_server.zones.typed_groups import (
    LocalGroup, SnapcastGroup, SonosGroup, TypedGroup, build_typed_groups,
)

if TYPE_CHECKING:
    from tune_server.db.engine import Database
    from tune_server.zones.zone import Zone

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# group_delays repo
# ---------------------------------------------------------------------------


def _canonical_pair(a: OutputType | str, b: OutputType | str) -> tuple[str, str]:
    """Sort pair alphabetically so (a,b) and (b,a) collapse to one row."""
    av = a.value if isinstance(a, OutputType) else str(a)
    bv = b.value if isinstance(b, OutputType) else str(b)
    return tuple(sorted([av, bv]))  # type: ignore[return-value]


class GroupDelayRepo:
    """Stores per-pair inter-techno delays. Single read/write per pair —
    no ORM, just direct SQL via the existing Database wrapper."""

    def __init__(self, db: "Database") -> None:
        self._db = db

    async def get_delay_ms(self, a: OutputType | str, b: OutputType | str) -> int:
        pa, pb = _canonical_pair(a, b)
        if pa == pb:
            return 0
        row = await self._db.fetchone(
            "SELECT delay_ms FROM group_delays WHERE tech_a = ? AND tech_b = ?",
            (pa, pb),
        )
        return int(row["delay_ms"]) if row else 0

    async def set_delay_ms(
        self, a: OutputType | str, b: OutputType | str, delay_ms: int,
    ) -> None:
        pa, pb = _canonical_pair(a, b)
        if pa == pb:
            return
        # Upsert pattern that works on both SQLite and PostgreSQL:
        # try INSERT, then UPDATE on conflict via the UNIQUE(tech_a,tech_b)
        # constraint. Avoids dialect-specific ON CONFLICT syntax.
        existing = await self._db.fetchone(
            "SELECT id FROM group_delays WHERE tech_a = ? AND tech_b = ?",
            (pa, pb),
        )
        if existing:
            await self._db.execute(
                "UPDATE group_delays SET delay_ms = ?, calibrated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (delay_ms, existing["id"]),
            )
        else:
            await self._db.execute(
                "INSERT INTO group_delays (tech_a, tech_b, delay_ms) VALUES (?, ?, ?)",
                (pa, pb, delay_ms),
            )
        await self._db.commit()

    async def list_all(self) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT tech_a, tech_b, delay_ms, calibrated_at FROM group_delays "
            "ORDER BY tech_a, tech_b",
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# CompositeGroup
# ---------------------------------------------------------------------------


@dataclass
class CompositeGroup:
    """Composes 1+ TypedGroups across technologies + applies one
    inter-group delay per pair when playback starts.

    Lifecycle:
      - `from_zones(zones)` factory groups input zones by techno into
        TypedGroups, returns CompositeGroup wrapping them.
      - `start_playback(...)` joins each typed group natively and
        returns the per-group `start_at_ms` offsets the player should
        use when launching streams (so the slowest technology starts
        first, others follow with their canonical delay).
      - `dissolve()` tears down all underlying typed groups.
    """

    name: str
    typed_groups: list[TypedGroup] = field(default_factory=list)

    @classmethod
    def from_zones(cls, name: str, zones: Iterable["Zone"]) -> "CompositeGroup":
        return cls(name=name, typed_groups=build_typed_groups(zones))

    @property
    def technologies(self) -> list[OutputType]:
        return [g.technology for g in self.typed_groups]

    @property
    def is_homogeneous(self) -> bool:
        techs = {g.technology for g in self.typed_groups}
        return len(techs) <= 1

    async def start_playback(
        self,
        manager_bag: dict,
        zones_by_id: dict[int, "Zone"],
        delay_repo: GroupDelayRepo,
    ) -> dict[OutputType, int]:
        """Activate all typed groups + compute per-group start offsets.

        Returns a `{technology: delay_ms}` dict telling the caller
        how long to wait before kicking off each group's playback.
        The slowest technology gets `delay_ms=0` (starts immediately);
        faster techs are delayed by their canonical pair offset so
        they "wait" for the slowest. This is calibrated once per pair
        — not per track.

        Homogeneous groups always return `{tech: 0}` — no
        cross-technology coordination needed.
        """
        # Native group join first.
        for g in self.typed_groups:
            await g.join(manager_bag, zones_by_id)

        if self.is_homogeneous:
            tech = self.technologies[0] if self.technologies else OutputType.LOCAL
            return {tech: 0}

        # For each pair, look up the canonical delay; the technology
        # that lags the most (largest sum of pair delays) starts at 0,
        # everyone else delays by `slowest_total - their_total`.
        delays_per_tech: dict[OutputType, int] = {t: 0 for t in self.technologies}
        for i, g_a in enumerate(self.typed_groups):
            for g_b in self.typed_groups[i + 1:]:
                d = await delay_repo.get_delay_ms(g_a.technology, g_b.technology)
                # Convention: positive delay means tech_b lags tech_a.
                # Canonical ordering is alphabetical; we add it onto
                # whichever side is "behind" in the pair.
                ca, cb = _canonical_pair(g_a.technology, g_b.technology)
                if ca == g_a.technology.value:
                    # g_a is the alphabetic first. delay_ms > 0 = g_b late.
                    delays_per_tech[g_b.technology] += max(d, 0)
                    delays_per_tech[g_a.technology] += max(-d, 0)
                else:
                    delays_per_tech[g_a.technology] += max(d, 0)
                    delays_per_tech[g_b.technology] += max(-d, 0)
        return delays_per_tech

    async def dissolve(
        self, manager_bag: dict, zones_by_id: dict[int, "Zone"],
    ) -> None:
        for g in self.typed_groups:
            await g.dissolve(manager_bag, zones_by_id)

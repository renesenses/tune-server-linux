"""Gapless multi-room coordinator.

Ensures all zones in a group transition gaplessly to the next track.
If ANY zone in the group doesn't support gapless, the entire group
falls back to sequential stop-then-play transitions.

Decision: gapless parfait ou rien (validated with Bertrand).
"""

from __future__ import annotations

import asyncio

import structlog

from tune_server.zones.zone import ZoneInstance

logger = structlog.get_logger()


async def check_group_gapless(zones: list[ZoneInstance]) -> bool:
    """Check if ALL zones in a group support gapless playback.

    Returns True only if every zone's output supports SetNextAVTransportURI
    (DLNA) or equivalent. If any zone doesn't, returns False.
    """
    for zone in zones:
        if zone.output is None:
            return False
        caps = getattr(zone.output, 'capabilities', None)
        if caps is None:
            return False
        if not getattr(caps, 'supports_gapless', False):
            logger.info("gapless_not_supported", zone=zone.name, zone_id=zone.zone_id)
            return False
    return True


async def prepare_group_gapless(
    zones: list[ZoneInstance],
    next_track,
    stream_info=None,
) -> bool:
    """Prepare all zones in a group for gapless transition.

    Calls set_next_track() on every zone's output. If any fails,
    the group falls back to non-gapless.

    Args:
        zones: All zones in the group (leader + followers)
        next_track: The Track object for the next song
        stream_info: AudioStreamInfo for the next track

    Returns:
        True if all zones are prepared for gapless, False otherwise.
    """
    if not await check_group_gapless(zones):
        logger.info("group_gapless_disabled", reason="not all zones support gapless")
        return False

    results = await asyncio.gather(
        *(zone.output.set_next_track(stream_info, next_track) for zone in zones),
        return_exceptions=True,
    )

    all_ok = all(r is True for r in results)
    if not all_ok:
        failed = [zones[i].name for i, r in enumerate(results) if r is not True]
        logger.warning("group_gapless_partial_failure", failed_zones=failed)
        # All-or-nothing: if any failed, cancel gapless for the group
        return False

    logger.info(
        "group_gapless_prepared",
        zones=[z.name for z in zones],
        next_track=getattr(next_track, 'title', ''),
    )
    return True

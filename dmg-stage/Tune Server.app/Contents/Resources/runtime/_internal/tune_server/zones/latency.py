"""Latency measurement for zone sync calibration.

Measures round-trip time to each output device to automatically
set sync_delay_ms for <50ms synchronization accuracy.
"""

from __future__ import annotations

import asyncio
import time

import structlog

from tune_server.zones.zone import ZoneInstance

logger = structlog.get_logger()


class LatencyMeasurer:
    """Measures output device latency for sync calibration."""

    @staticmethod
    async def measure_zone_latency(zone: ZoneInstance, samples: int = 5) -> int | None:
        """Measure round-trip latency to a zone's output device.

        Returns estimated one-way latency in milliseconds, or None if
        measurement failed.

        Strategy:
        - For DLNA: time a GetPositionInfo RPC call (network round-trip / 2)
        - For AirPlay: time a metadata query
        - For Local: assume ~10ms (kernel buffer + DAC)
        """
        output = zone.output
        if output is None:
            return None

        output_type = zone.output_type

        if output_type == "local":
            return 10  # PortAudio kernel buffer latency

        # Measure network round-trip via position query.
        # get_position_ms() calls the renderer's UPnP endpoint regardless of
        # playback state, so the RTT can be measured on stopped zones too.
        latencies = []
        for _ in range(samples):
            try:
                start = time.monotonic()
                await asyncio.wait_for(
                    output.get_position_ms(),
                    timeout=2.0,
                )
                elapsed_ms = (time.monotonic() - start) * 1000
                # One-way latency ≈ round-trip / 2
                latencies.append(elapsed_ms / 2)
            except Exception:
                pass
            await asyncio.sleep(0.1)

        if not latencies:
            return None

        # Use median to filter outliers
        latencies.sort()
        median_idx = len(latencies) // 2
        median_latency = latencies[median_idx]

        logger.info(
            "latency_measured",
            zone=zone.name,
            zone_id=zone.zone_id,
            samples=len(latencies),
            median_ms=round(median_latency, 1),
            min_ms=round(min(latencies), 1),
            max_ms=round(max(latencies), 1),
        )

        return round(median_latency)

    @staticmethod
    async def auto_calibrate_group(
        leader: ZoneInstance,
        followers: list[ZoneInstance],
        db=None,
    ) -> dict[int, int]:
        """Measure latency for all zones in a group and set sync_delay_ms.

        The leader's latency becomes the reference. Each follower's
        sync_delay_ms is set to (leader_latency - follower_latency) so
        that all outputs receive audio at approximately the same time.

        Returns: {zone_id: new_sync_delay_ms}
        """
        results = {}

        leader_latency = await LatencyMeasurer.measure_zone_latency(leader)
        if leader_latency is None:
            logger.warning("calibration_failed_leader", zone=leader.name)
            return results

        results[leader.zone_id] = 0  # Leader is reference

        for follower in followers:
            follower_latency = await LatencyMeasurer.measure_zone_latency(follower)
            if follower_latency is None:
                logger.warning("calibration_failed_follower", zone=follower.name)
                continue

            # Offset = leader - follower
            # Positive = follower should start later (closer/faster)
            # Negative = follower should start earlier (farther/slower)
            offset = leader_latency - follower_latency
            follower.sync_delay_ms = offset
            results[follower.zone_id] = offset

            # Persist to DB
            if db:
                try:
                    await db.execute(
                        "UPDATE zones SET sync_delay_ms = ? WHERE id = ?",
                        (offset, follower.zone_id),
                    )
                    await db.commit()
                except Exception:
                    pass

            logger.info(
                "calibration_result",
                zone=follower.name,
                zone_id=follower.zone_id,
                leader_latency_ms=leader_latency,
                follower_latency_ms=follower_latency,
                sync_delay_ms=offset,
            )

        return results


class ZoneHealthMonitor:
    """Monitors zone health and reports status."""

    @staticmethod
    async def check_zone_health(zone: ZoneInstance) -> dict:
        """Check health of a zone's output device.

        Returns:
            {
                "zone_id": int,
                "name": str,
                "status": "online" | "offline" | "degraded",
                "latency_ms": int | None,
                "position_ok": bool,
                "details": str,
            }
        """
        result = {
            "zone_id": zone.zone_id,
            "name": zone.name,
            "status": "offline",
            "latency_ms": None,
            "position_ok": False,
            "details": "",
        }

        if zone.output is None:
            result["details"] = "No output configured"
            return result

        # Check connectivity via position query
        try:
            start = time.monotonic()
            pos = await asyncio.wait_for(
                zone.output.get_position_ms(),
                timeout=3.0,
            )
            latency = (time.monotonic() - start) * 1000

            result["status"] = "online"
            result["latency_ms"] = round(latency)
            result["position_ok"] = pos >= 0

            if latency > 500:
                result["status"] = "degraded"
                result["details"] = f"High latency: {latency:.0f}ms"
            elif pos < 0:
                result["details"] = "Position unavailable"
            else:
                result["details"] = f"OK ({latency:.0f}ms)"

        except asyncio.TimeoutError:
            result["details"] = "Timeout (device unreachable)"
        except Exception as e:
            result["details"] = f"Error: {str(e)[:60]}"

        return result

    @staticmethod
    async def check_group_health(
        leader: ZoneInstance,
        followers: list[ZoneInstance],
    ) -> dict:
        """Check health of all zones in a group."""
        zones = [leader] + followers
        health = await asyncio.gather(
            *(ZoneHealthMonitor.check_zone_health(z) for z in zones)
        )

        all_online = all(h["status"] == "online" for h in health)
        any_degraded = any(h["status"] == "degraded" for h in health)
        any_offline = any(h["status"] == "offline" for h in health)

        return {
            "group_status": "online" if all_online else "degraded" if any_degraded else "offline" if any_offline else "mixed",
            "zones": health,
            "gapless_capable": all(
                h["status"] == "online" and h["position_ok"] for h in health
            ),
        }

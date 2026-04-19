from __future__ import annotations

import asyncio
import uuid

import structlog

from tune_server.db.engine import Database
from tune_server.db.repository import PlayQueueRepo, ZoneRepo
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import OutputType
from tune_server.outputs.base import OutputTarget
from tune_server.outputs.local import LocalOutput
from tune_server.zones.zone import ZoneInstance

logger = structlog.get_logger()


class ZoneManager:
    """Manages zone lifecycle and provides access to active zones."""

    def __init__(self, db: Database, event_bus: EventBus) -> None:
        self._db = db
        self._event_bus = event_bus
        self._zone_repo = ZoneRepo(db)
        self._queue_repo = PlayQueueRepo(db)
        self._zones: dict[int, ZoneInstance] = {}
        self._output_factory: dict[OutputType, callable] = {}
        self._pending_zones: list = []
        self._stream_url_resolver = None
        self._group_manager = None
        # Map zone_id → bool remembering whether a zone was playing when
        # its device went offline, so we can resume on recovery.
        self._resume_on_recovery: dict[int, bool] = {}

    def set_stream_url_resolver(self, resolver) -> None:
        self._stream_url_resolver = resolver

    def set_group_manager(self, group_manager) -> None:
        self._group_manager = group_manager

    def register_output_factory(self, output_type: OutputType, factory: callable) -> None:
        self._output_factory[output_type] = factory

    async def initialize(self) -> None:
        """Load persisted zones from DB and create instances."""
        # Listen for device discovery changes to mark zones online/offline
        self._event_bus.on(EventType.DEVICE_LOST, self._on_device_lost)
        self._event_bus.on(EventType.DEVICE_DISCOVERED, self._on_device_discovered)

        zone_rows = await self._zone_repo.list()
        for row in zone_rows:
            try:
                zone_id = row["id"]
                output_type = OutputType(row["output_type"])
                output = await self._create_output(
                    output_type, row.get("output_device_id")
                )
                if output:
                    zone = ZoneInstance(
                        zone_id=zone_id,
                        name=row["name"],
                        output_type=output_type,
                        output=output,
                        event_bus=self._event_bus,
                        queue_repo=self._queue_repo,
                        zone_repo=self._zone_repo,
                        output_device_id=row.get("output_device_id"),
                    )
                    zone.group_id = row.get("group_id")
                    zone.sync_delay_ms = row.get("sync_delay_ms", 0) or 0
                    zone.stereo_pair_id = row.get("stereo_pair_id")
                    zone.stereo_channel = row.get("stereo_channel")
                    if self._stream_url_resolver:
                        zone.player.set_stream_url_resolver(self._stream_url_resolver)

                    # Restore persisted volume
                    saved_volume = row.get("volume", 0.5)
                    if saved_volume is not None and saved_volume != 0.5:
                        await zone.player.set_volume(saved_volume)

                    # Restore persisted queue
                    await zone.restore_queue()

                    self._zones[zone_id] = zone
                    logger.info("zone_loaded", id=zone_id, name=row["name"])
                else:
                    # Output device not available — retry later
                    logger.warning("zone_output_unavailable", id=zone_id, name=row["name"],
                                   output_type=output_type, device=row.get("output_device_id"))
                    self._pending_zones.append(row)
            except Exception:
                logger.exception("zone_load_error", id=row["id"])

    async def retry_pending_zones(self) -> None:
        """Retry loading zones whose output was not available at startup."""
        if not self._pending_zones:
            return
        remaining = []
        for row in self._pending_zones:
            try:
                zone_id = row["id"]
                if zone_id in self._zones:
                    continue
                output_type = OutputType(row["output_type"])
                output = await self._create_output(output_type, row.get("output_device_id"))
                if output:
                    zone = ZoneInstance(
                        zone_id=zone_id, name=row["name"], output_type=output_type,
                        output=output, event_bus=self._event_bus,
                        queue_repo=self._queue_repo, zone_repo=self._zone_repo,
                        output_device_id=row.get("output_device_id"),
                    )
                    zone.group_id = row.get("group_id")
                    zone.sync_delay_ms = row.get("sync_delay_ms", 0) or 0
                    zone.stereo_pair_id = row.get("stereo_pair_id")
                    zone.stereo_channel = row.get("stereo_channel")
                    if self._stream_url_resolver:
                        zone.player.set_stream_url_resolver(self._stream_url_resolver)
                    saved_volume = row.get("volume", 0.5)
                    if saved_volume is not None and saved_volume != 0.5:
                        await zone.player.set_volume(saved_volume)
                    await zone.restore_queue()
                    self._zones[zone_id] = zone
                    logger.info("zone_loaded_retry", id=zone_id, name=row["name"])
                else:
                    remaining.append(row)
            except Exception:
                remaining.append(row)
        self._pending_zones = remaining

    async def create_zone(
        self,
        name: str,
        output_type: OutputType,
        output_device_id: str | None = None,
        sync_delay_ms: int = 0,
    ) -> ZoneInstance:
        # Prevent duplicate device assignment
        if output_device_id:
            for zone in self._zones.values():
                if (zone.output_type == output_type
                        and zone.output_device_id == output_device_id):
                    raise ValueError(
                        f"Device already in use by zone '{zone.name}'"
                    )

        # Create output FIRST — only persist to DB if it succeeds
        output = await self._create_output(output_type, output_device_id)
        if not output:
            raise RuntimeError(f"Could not create output for type {output_type}")

        # Persist to DB
        zone_id = await self._zone_repo.create(name, output_type.value, output_device_id)
        if sync_delay_ms:
            await self._zone_repo.update(zone_id, sync_delay_ms=sync_delay_ms)

        zone = ZoneInstance(
            zone_id=zone_id,
            name=name,
            output_type=output_type,
            output=output,
            event_bus=self._event_bus,
            queue_repo=self._queue_repo,
            zone_repo=self._zone_repo,
            output_device_id=output_device_id,
        )
        zone.sync_delay_ms = sync_delay_ms
        if self._stream_url_resolver:
            zone.player.set_stream_url_resolver(self._stream_url_resolver)
        self._zones[zone_id] = zone

        await self._event_bus.emit(Event(
            type=EventType.ZONE_CREATED,
            data={"zone_id": zone_id, "name": name},
            source="zone_manager",
        ))

        logger.info("zone_created", id=zone_id, name=name, type=output_type)
        return zone

    async def rename_zone(self, zone_id: int, name: str) -> ZoneInstance:
        return await self.update_zone(zone_id, name=name)

    async def update_zone(
        self,
        zone_id: int,
        name: str | None = None,
        sync_delay_ms: int | None = None,
    ) -> ZoneInstance:
        zone = self._zones.get(zone_id)
        if not zone:
            raise KeyError(f"Zone {zone_id} not found")

        db_updates: dict = {}
        event_data: dict = {"zone_id": zone_id}

        if name is not None:
            zone.name = name
            db_updates["name"] = name
            event_data["name"] = name

        if sync_delay_ms is not None:
            zone.sync_delay_ms = sync_delay_ms
            db_updates["sync_delay_ms"] = sync_delay_ms
            event_data["sync_delay_ms"] = sync_delay_ms

        if db_updates:
            await self._zone_repo.update(zone_id, **db_updates)
            await self._event_bus.emit(Event(
                type=EventType.ZONE_UPDATED,
                data=event_data,
                source="zone_manager",
            ))
            logger.info("zone_updated", id=zone_id, **db_updates)
        return zone

    async def delete_zone(self, zone_id: int) -> None:
        zone = self._zones.pop(zone_id, None)
        if zone:
            await zone.cleanup()
        await self._zone_repo.delete(zone_id)

        await self._event_bus.emit(Event(
            type=EventType.ZONE_DELETED,
            data={"zone_id": zone_id},
            source="zone_manager",
        ))

    async def set_output(
        self,
        zone_id: int,
        output_type: OutputType | str,
        output_device_id: str | None = None,
    ) -> ZoneInstance:
        """Hot-swap a zone's output target without recreating the zone.

        Rejects the swap if another zone already uses the same (type, device).
        """
        zone = self._zones.get(zone_id)
        if not zone:
            raise KeyError(f"Zone {zone_id} not found")

        if isinstance(output_type, str):
            output_type = OutputType(output_type)

        # Prevent duplicate device assignment (exclude the zone being swapped)
        if output_device_id:
            for other in self._zones.values():
                if other.zone_id == zone_id:
                    continue
                if (other.output_type == output_type
                        and other.output_device_id == output_device_id):
                    raise ValueError(
                        f"Device already in use by zone '{other.name}'"
                    )

        # No-op if the target is identical
        if (zone.output_type == output_type
                and zone.output_device_id == output_device_id):
            return zone

        # Create the new output first — fail fast before touching the zone
        new_output = await self._create_output(output_type, output_device_id)
        if not new_output:
            raise RuntimeError(
                f"Could not create output for type={output_type} device={output_device_id}"
            )

        await zone.update_output(output_type, new_output, output_device_id)

        # Persist the change
        await self._zone_repo.update(
            zone_id,
            output_type=output_type.value,
            output_device_id=output_device_id,
        )

        await self._event_bus.emit(Event(
            type=EventType.ZONE_UPDATED,
            data={
                "zone_id": zone_id,
                "output_type": output_type.value,
                "output_device_id": output_device_id,
            },
            source="zone_manager",
        ))

        logger.info(
            "zone_output_swapped",
            id=zone_id,
            type=output_type.value,
            device=output_device_id,
        )
        return zone

    def get_zone(self, zone_id: int) -> ZoneInstance | None:
        return self._zones.get(zone_id)

    def list_zones(self) -> list[ZoneInstance]:
        return list(self._zones.values())

    async def _create_output(
        self, output_type: OutputType, device_id: str | None
    ) -> OutputTarget | None:
        # Check registered factories first
        if output_type in self._output_factory:
            return await self._output_factory[output_type](device_id)

        # Default: local output
        if output_type == OutputType.LOCAL:
            return LocalOutput(device_name=device_id)

        logger.warning("no_output_factory", type=output_type)
        return None

    # -----------------------------------------------------------------
    # Stereo pairing
    # -----------------------------------------------------------------

    async def create_stereo_pair(
        self, name: str, left_device_id: str, right_device_id: str,
    ) -> str:
        """Create a stereo pair from two DLNA devices (left + right channels)."""
        stereo_pair_id = str(uuid.uuid4())

        # Create left and right zones
        left_zone = await self.create_zone(
            f"{name} (L)", OutputType.DLNA, output_device_id=left_device_id,
        )
        right_zone = await self.create_zone(
            f"{name} (R)", OutputType.DLNA, output_device_id=right_device_id,
        )

        # Set stereo pair metadata on both zones
        left_zone.stereo_pair_id = stereo_pair_id
        left_zone.stereo_channel = "left"
        right_zone.stereo_pair_id = stereo_pair_id
        right_zone.stereo_channel = "right"

        # Persist stereo fields to DB
        await self._zone_repo.update(
            left_zone.zone_id,
            stereo_pair_id=stereo_pair_id, stereo_channel="left",
        )
        await self._zone_repo.update(
            right_zone.zone_id,
            stereo_pair_id=stereo_pair_id, stereo_channel="right",
        )

        # Group them: left is leader, right is follower
        if self._group_manager:
            await self._group_manager.create_group(left_zone, [right_zone])

        logger.info(
            "stereo_pair_created",
            pair_id=stereo_pair_id,
            name=name,
            left_zone=left_zone.zone_id,
            right_zone=right_zone.zone_id,
        )
        return stereo_pair_id

    async def dissolve_stereo_pair(self, stereo_pair_id: str) -> None:
        """Dissolve a stereo pair — ungroup and delete both zones."""
        zones = [
            z for z in self._zones.values()
            if z.stereo_pair_id == stereo_pair_id
        ]
        if not zones:
            raise KeyError(f"Stereo pair {stereo_pair_id} not found")

        # Dissolve the group first
        if self._group_manager and zones[0].group_id:
            await self._group_manager.dissolve_group(zones[0].group_id)

        # Delete both zones
        for zone in zones:
            await self.delete_zone(zone.zone_id)

        logger.info("stereo_pair_dissolved", pair_id=stereo_pair_id)

    def get_stereo_pairs(self) -> list[dict]:
        """Return all active stereo pairs."""
        pairs: dict[str, dict] = {}
        for zone in self._zones.values():
            if not zone.stereo_pair_id:
                continue
            pair_id = zone.stereo_pair_id
            if pair_id not in pairs:
                pairs[pair_id] = {
                    "stereo_pair_id": pair_id,
                    "name": None,
                    "left_zone": None,
                    "right_zone": None,
                }
            entry = pairs[pair_id]
            if zone.stereo_channel == "left":
                entry["left_zone"] = zone.to_model()
                # Derive pair name from zone name (strip " (L)" suffix)
                raw = zone.name
                if raw.endswith(" (L)"):
                    entry["name"] = raw[:-4]
                else:
                    entry["name"] = entry["name"] or raw
            elif zone.stereo_channel == "right":
                entry["right_zone"] = zone.to_model()
                if entry["name"] is None:
                    raw = zone.name
                    entry["name"] = raw[:-4] if raw.endswith(" (R)") else raw
        return list(pairs.values())

    async def cleanup(self) -> None:
        for zone in self._zones.values():
            await zone.cleanup()
        self._zones.clear()

    # -----------------------------------------------------------------
    # Device availability listeners
    # -----------------------------------------------------------------

    async def _on_device_lost(self, event: Event) -> None:
        """A discovered device went offline — flag zones using it as offline
        and auto-pause playback so nothing blasts to a ghost output."""
        dev_id = event.data.get("id") if event.data else None
        if not dev_id:
            return
        from tune_server.models import PlaybackState
        for zone in self._zones.values():
            if zone.output_device_id != dev_id or not zone.online:
                continue
            zone.online = False
            # Remember if we were playing so we can resume on recovery
            was_playing = zone.player.state == PlaybackState.PLAYING
            self._resume_on_recovery[zone.zone_id] = was_playing

            if was_playing:
                try:
                    await zone.player.pause()
                    logger.info("zone_auto_paused", zone_id=zone.zone_id, device=dev_id)
                except Exception:
                    logger.exception("zone_auto_pause_error", zone_id=zone.zone_id)

            logger.info("zone_offline", zone_id=zone.zone_id, device=dev_id, was_playing=was_playing)
            await self._event_bus.emit(Event(
                type=EventType.ZONE_UPDATED,
                data={
                    "zone_id": zone.zone_id,
                    "online": False,
                    "error_code": "device_unavailable",
                    "was_playing": was_playing,
                },
                source="zone_manager",
            ))

    async def _on_device_discovered(self, event: Event) -> None:
        """A device came back online — flag zones and resume if they were playing."""
        dev_id = event.data.get("id") if event.data else None
        if not dev_id:
            return
        for zone in self._zones.values():
            if zone.output_device_id != dev_id or zone.online:
                continue
            zone.online = True
            should_resume = self._resume_on_recovery.pop(zone.zone_id, False)
            logger.info("zone_online", zone_id=zone.zone_id, device=dev_id, will_resume=should_resume)

            if should_resume:
                # Fire-and-forget retry loop: give the device a few seconds to
                # settle, then attempt resume. Log but don't crash on failure.
                asyncio.create_task(self._resume_zone_with_retry(zone))

            await self._event_bus.emit(Event(
                type=EventType.ZONE_UPDATED,
                data={"zone_id": zone.zone_id, "online": True, "resuming": should_resume},
                source="zone_manager",
            ))

    async def _resume_zone_with_retry(self, zone: "ZoneInstance") -> None:
        """Wait briefly for a recovered device then resume playback (best-effort)."""
        for delay in (2, 5, 10):
            await asyncio.sleep(delay)
            if not zone.online:
                return  # Device disappeared again
            try:
                await zone.player.resume()
                logger.info("zone_auto_resumed", zone_id=zone.zone_id)
                return
            except Exception as e:
                logger.warning("zone_resume_attempt_failed", zone_id=zone.zone_id, error=str(e))
        logger.warning("zone_resume_gave_up", zone_id=zone.zone_id)

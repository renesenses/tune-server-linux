from __future__ import annotations

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
        self._stream_url_resolver = None

    def set_stream_url_resolver(self, resolver) -> None:
        self._stream_url_resolver = resolver

    def register_output_factory(self, output_type: OutputType, factory: callable) -> None:
        self._output_factory[output_type] = factory

    async def initialize(self) -> None:
        """Load persisted zones from DB and create instances."""
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
                    )
                    zone.group_id = row.get("group_id")
                    zone.sync_delay_ms = row.get("sync_delay_ms", 0) or 0
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
                    # Output device not available — remove stale zone from DB
                    logger.warning("zone_stale_removed", id=zone_id, name=row["name"],
                                   output_type=output_type, device=row.get("output_device_id"))
                    await self._zone_repo.delete(zone_id)
            except Exception:
                logger.exception("zone_load_error", id=row["id"])

    async def create_zone(
        self,
        name: str,
        output_type: OutputType,
        output_device_id: str | None = None,
        sync_delay_ms: int = 0,
    ) -> ZoneInstance:
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

    async def cleanup(self) -> None:
        for zone in self._zones.values():
            await zone.cleanup()
        self._zones.clear()

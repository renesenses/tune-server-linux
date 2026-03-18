from __future__ import annotations

import asyncio
import socket
from typing import Optional

import structlog

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import DiscoveredDevice, OutputType

logger = structlog.get_logger()

AIRPLAY_SERVICE = "_raop._tcp.local."
AIRPLAY_SERVICE_ALT = "_airplay._tcp.local."


class MdnsDiscovery:
    """mDNS/Bonjour discovery for AirPlay devices."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._devices: dict[str, DiscoveredDevice] = {}
        self._atv_configs: dict[str, object] = {}  # device_id -> pyatv config
        self._zeroconf = None
        self._browser = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def devices(self) -> dict[str, DiscoveredDevice]:
        return dict(self._devices)

    def get_atv_config(self, device_id: str) -> Optional[object]:
        return self._atv_configs.get(device_id)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._discovery_loop())
        logger.info("mdns_discovery_started")

    async def stop(self) -> None:
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._zeroconf:
            try:
                await asyncio.to_thread(self._zeroconf.close)
            except Exception:
                pass

    async def _discovery_loop(self) -> None:
        try:
            import pyatv

            while self._running:
                try:
                    logger.debug("mdns_scanning")
                    atvs = await pyatv.scan(asyncio.get_running_loop(), timeout=10)

                    found_ids = set()
                    for atv_config in atvs:
                        dev_id = str(atv_config.identifier) or atv_config.name
                        found_ids.add(dev_id)

                        address = str(atv_config.address)
                        name = atv_config.name

                        disc_device = DiscoveredDevice(
                            id=dev_id,
                            name=name,
                            type=OutputType.AIRPLAY,
                            host=address,
                            port=7000,
                            available=True,
                            capabilities={"airplay": True},
                        )

                        async with self._lock:
                            was_lost = dev_id in self._devices and not self._devices[dev_id].available
                            is_new = dev_id not in self._devices
                            self._devices[dev_id] = disc_device
                            self._atv_configs[dev_id] = atv_config

                        if is_new or was_lost:
                            await self._event_bus.emit(Event(
                                type=EventType.DEVICE_DISCOVERED,
                                data=disc_device.model_dump(),
                                source="mdns",
                            ))
                            logger.info("airplay_device_found", name=name, id=dev_id, recovered=was_lost)

                    # Mark lost devices
                    async with self._lock:
                        for dev_id in list(self._devices.keys()):
                            if dev_id not in found_ids:
                                device = self._devices[dev_id]
                                if device.available:
                                    device.available = False
                                    await self._event_bus.emit(Event(
                                        type=EventType.DEVICE_LOST,
                                        data={"id": dev_id, "name": device.name},
                                        source="mdns",
                                    ))

                except Exception:
                    logger.exception("mdns_scan_error")

                await asyncio.sleep(30)

        except ImportError:
            logger.warning("pyatv_not_installed")
        except asyncio.CancelledError:
            raise

    async def rescan(self) -> list[DiscoveredDevice]:
        """Run a single mDNS scan immediately and return discovered devices."""
        try:
            import pyatv
        except ImportError:
            return []

        atvs = await pyatv.scan(asyncio.get_running_loop(), timeout=10)

        for atv_config in atvs:
            dev_id = str(atv_config.identifier) or atv_config.name
            address = str(atv_config.address)
            name = atv_config.name

            disc_device = DiscoveredDevice(
                id=dev_id,
                name=name,
                type=OutputType.AIRPLAY,
                host=address,
                port=7000,
                available=True,
                capabilities={"airplay": True},
            )

            async with self._lock:
                was_lost = dev_id in self._devices and not self._devices[dev_id].available
                is_new = dev_id not in self._devices
                self._devices[dev_id] = disc_device
                self._atv_configs[dev_id] = atv_config

            if is_new or was_lost:
                await self._event_bus.emit(Event(
                    type=EventType.DEVICE_DISCOVERED,
                    data=disc_device.model_dump(),
                    source="mdns",
                ))
                logger.info("airplay_device_found", name=name, id=dev_id, recovered=was_lost)

        return list(self._devices.values())

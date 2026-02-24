from __future__ import annotations

import structlog

from tune_server.config import settings
from tune_server.discovery.mdns import MdnsDiscovery
from tune_server.discovery.ssdp import SsdpDiscovery
from tune_server.event_bus import EventBus
from tune_server.models import DiscoveredDevice

logger = structlog.get_logger()


class DiscoveryManager:
    """Unified device discovery registry combining SSDP and mDNS."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._ssdp = SsdpDiscovery(event_bus) if settings.ssdp_enabled else None
        self._mdns = MdnsDiscovery(event_bus) if settings.mdns_enabled else None

    @property
    def ssdp(self) -> SsdpDiscovery | None:
        return self._ssdp

    @property
    def mdns(self) -> MdnsDiscovery | None:
        return self._mdns

    async def start(self) -> None:
        if not settings.discovery_enabled:
            logger.info("discovery_disabled")
            return

        if self._ssdp:
            await self._ssdp.start()
        if self._mdns:
            await self._mdns.start()

        logger.info("discovery_manager_started")

    async def stop(self) -> None:
        if self._ssdp:
            await self._ssdp.stop()
        if self._mdns:
            await self._mdns.stop()

    def list_devices(self) -> list[DiscoveredDevice]:
        devices: list[DiscoveredDevice] = []
        if self._ssdp:
            devices.extend(self._ssdp.devices.values())
        if self._mdns:
            devices.extend(self._mdns.devices.values())
        return devices

    def get_device(self, device_id: str) -> DiscoveredDevice | None:
        if self._ssdp:
            dev = self._ssdp.devices.get(device_id)
            if dev:
                return dev
        if self._mdns:
            dev = self._mdns.devices.get(device_id)
            if dev:
                return dev
        return None

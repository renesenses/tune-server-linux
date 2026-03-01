from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from tune_server.config import settings
from tune_server.discovery.mdns import MdnsDiscovery
from tune_server.discovery.ssdp import SsdpDiscovery
from tune_server.event_bus import EventBus
from tune_server.models import DiscoveredDevice

if TYPE_CHECKING:
    from tune_server.discovery.media_servers import MediaServerDiscovery
    from tune_server.discovery.network_shares import NetworkShareDiscovery

logger = structlog.get_logger()


class DiscoveryManager:
    """Unified device discovery registry combining SSDP, mDNS, network shares, and media servers."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._ssdp = SsdpDiscovery(event_bus) if settings.ssdp_enabled else None
        self._mdns = MdnsDiscovery(event_bus) if settings.mdns_enabled else None
        self._network_shares: NetworkShareDiscovery | None = None
        self._media_servers: MediaServerDiscovery | None = None

        if settings.network_shares_enabled:
            from tune_server.discovery.network_shares import NetworkShareDiscovery as NSDisc
            self._network_shares = NSDisc(event_bus)

        if settings.network_media_servers_enabled:
            from tune_server.discovery.media_servers import MediaServerDiscovery as MSDisc
            self._media_servers = MSDisc(event_bus)

    @property
    def ssdp(self) -> SsdpDiscovery | None:
        return self._ssdp

    @property
    def mdns(self) -> MdnsDiscovery | None:
        return self._mdns

    @property
    def network_shares(self) -> NetworkShareDiscovery | None:
        return self._network_shares

    @property
    def media_servers(self) -> MediaServerDiscovery | None:
        return self._media_servers

    async def start(self) -> None:
        if not settings.discovery_enabled:
            logger.info("discovery_disabled")
            return

        if self._ssdp:
            await self._ssdp.start()
        if self._mdns:
            await self._mdns.start()
        if self._network_shares:
            await self._network_shares.start()
        if self._media_servers:
            await self._media_servers.start()

        logger.info("discovery_manager_started")

    async def stop(self) -> None:
        if self._ssdp:
            await self._ssdp.stop()
        if self._mdns:
            await self._mdns.stop()
        if self._network_shares:
            await self._network_shares.stop()
        if self._media_servers:
            await self._media_servers.stop()

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

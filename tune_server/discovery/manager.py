from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from tune_server.config import settings
from tune_server.discovery.bluos import BluosDiscovery
from tune_server.discovery.cast import CastDiscovery
from tune_server.discovery.mdns import MdnsDiscovery
from tune_server.discovery.openhome import OpenHomeDiscovery
from tune_server.discovery.ssdp import SsdpDiscovery
from tune_server.discovery.tune_discovery import TuneDiscovery
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
        self._shared_zc = None
        self._ssdp = SsdpDiscovery(event_bus) if settings.ssdp_enabled else None
        self._mdns = MdnsDiscovery(event_bus) if settings.mdns_enabled else None
        self._cast = CastDiscovery(event_bus) if getattr(settings, 'cast_enabled', True) else None
        self._bluos = BluosDiscovery(event_bus) if getattr(settings, 'bluos_enabled', True) else None
        self._openhome = OpenHomeDiscovery(event_bus) if settings.ssdp_enabled else None
        self._tune = TuneDiscovery(event_bus) if settings.peer_discovery_enabled else None
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
    def cast(self) -> CastDiscovery | None:
        return self._cast

    @property
    def bluos(self) -> BluosDiscovery | None:
        return self._bluos

    @property
    def openhome(self) -> OpenHomeDiscovery | None:
        return self._openhome

    @property
    def tune(self) -> TuneDiscovery | None:
        return self._tune

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

        # Create a shared Zeroconf instance for mDNS + Cast + BluOS + Tune peer discovery
        # to avoid port 5353 conflicts from multiple instances.
        if self._mdns or self._cast or self._bluos or self._tune:
            try:
                from zeroconf import Zeroconf
                self._shared_zc = Zeroconf()
            except Exception:
                logger.warning("shared_zeroconf_init_failed")

        if self._ssdp:
            await self._ssdp.start()
        if self._mdns:
            await self._mdns.start(shared_zc=self._shared_zc)
        if self._cast:
            await self._cast.start(shared_zc=self._shared_zc)
        if self._bluos:
            await self._bluos.start(shared_zc=self._shared_zc)
        if self._openhome:
            await self._openhome.start()
        if self._tune:
            await self._tune.start(shared_zc=self._shared_zc)
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
        if self._cast:
            await self._cast.stop()
        if self._bluos:
            await self._bluos.stop()
        if self._openhome:
            await self._openhome.stop()
        if self._tune:
            await self._tune.stop()
        if self._shared_zc:
            self._shared_zc.close()
            self._shared_zc = None
        if self._network_shares:
            await self._network_shares.stop()
        if self._media_servers:
            await self._media_servers.stop()

    _TYPE_PRIORITY = {
        "openhome": 6, "bluos": 5, "chromecast": 4,
        "airplay": 3, "dlna": 2, "local": 1,
    }

    def list_devices(self) -> list[DiscoveredDevice]:
        devices: list[DiscoveredDevice] = []
        if self._ssdp:
            devices.extend(self._ssdp.devices.values())
        if self._mdns:
            devices.extend(self._mdns.devices.values())
        if self._cast:
            devices.extend(self._cast.devices.values())
        if self._bluos:
            devices.extend(self._bluos.devices.values())
        if self._openhome:
            devices.extend(self._openhome.devices.values())
        return devices

    def list_devices_deduped(self) -> list[DiscoveredDevice]:
        """Deduplicated device list for UI display.

        When the same IP is discovered via multiple protocols, keep only
        the most specific one (OpenHome > BluOS > Cast > AirPlay > DLNA).
        """
        all_devices = self.list_devices()
        by_host: dict[str, DiscoveredDevice] = {}
        for dev in all_devices:
            existing = by_host.get(dev.host)
            if not existing:
                by_host[dev.host] = dev
            else:
                new_prio = self._TYPE_PRIORITY.get(dev.type.value, 0)
                old_prio = self._TYPE_PRIORITY.get(existing.type.value, 0)
                if new_prio > old_prio:
                    by_host[dev.host] = dev
        return list(by_host.values())

    async def rescan(self) -> list[DiscoveredDevice]:
        """Force an immediate rescan of all discovery sources."""
        logger.info("discovery_rescan_requested")
        tasks = []
        if self._ssdp:
            tasks.append(self._ssdp.rescan())
        if self._mdns:
            tasks.append(self._mdns.rescan())
        if self._cast:
            tasks.append(self._cast.rescan())
        if self._bluos:
            tasks.append(self._bluos.rescan())
        if self._openhome:
            tasks.append(self._openhome.rescan())
        if tasks:
            await asyncio.gather(*tasks)
        return self.list_devices()

    def get_device(self, device_id: str) -> DiscoveredDevice | None:
        if self._ssdp:
            dev = self._ssdp.devices.get(device_id)
            if dev:
                return dev
        if self._mdns:
            dev = self._mdns.devices.get(device_id)
            if dev:
                return dev
        if self._cast:
            dev = self._cast.devices.get(device_id)
            if dev:
                return dev
        if self._bluos:
            dev = self._bluos.devices.get(device_id)
            if dev:
                return dev
        return None

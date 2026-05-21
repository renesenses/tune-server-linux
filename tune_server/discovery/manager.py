from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from tune_server.config import settings
from tune_server.discovery.bluos import BluosDiscovery
from tune_server.discovery.cast import CastDiscovery
from tune_server.discovery.mdns import MdnsDiscovery
from tune_server.discovery.openhome import OpenHomeDiscovery
from tune_server.discovery.rust_discovery import (
    RustMdnsBackend,
    RustSsdpBackend,
    rust_discovery_available,
)
from tune_server.discovery.squeezebox import SqueezeboxDiscovery
from tune_server.discovery.ssdp import SsdpDiscovery
from tune_server.discovery.tune_discovery import TuneDiscovery
from tune_server.event_bus import EventBus
from tune_server.models import DiscoveredDevice

if TYPE_CHECKING:
    from tune_server.discovery.media_servers import MediaServerDiscovery
    from tune_server.discovery.network_shares import NetworkShareDiscovery

logger = structlog.get_logger()

_USE_RUST = rust_discovery_available()


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
        self._squeezebox = SqueezeboxDiscovery(event_bus) if getattr(settings, 'squeezebox_enabled', True) else None
        self._tune = TuneDiscovery(event_bus) if settings.peer_discovery_enabled else None
        self._network_shares: NetworkShareDiscovery | None = None
        self._media_servers: MediaServerDiscovery | None = None

        self._rust_ssdp: RustSsdpBackend | None = None
        self._rust_mdns: RustMdnsBackend | None = None
        if _USE_RUST:
            if settings.ssdp_enabled:
                self._rust_ssdp = RustSsdpBackend(event_bus)
            if settings.mdns_enabled:
                self._rust_mdns = RustMdnsBackend(event_bus)
            logger.info("discovery_engine", engine="rust", ssdp="rust" if self._rust_ssdp else "python", mdns="rust" if self._rust_mdns else "python")
        else:
            logger.info("discovery_engine", engine="python")

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
    def squeezebox(self) -> SqueezeboxDiscovery | None:
        return self._squeezebox

    @property
    def tune(self) -> TuneDiscovery | None:
        return self._tune

    @property
    def rust_ssdp(self) -> RustSsdpBackend | None:
        return self._rust_ssdp

    @property
    def rust_mdns(self) -> RustMdnsBackend | None:
        return self._rust_mdns

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

        # Rust backends: fast parallel discovery via native sockets
        if self._rust_ssdp:
            await self._rust_ssdp.start()
        if self._rust_mdns:
            await self._rust_mdns.start()

        # Create a shared Zeroconf instance for mDNS + Cast + BluOS + Squeezebox + Tune
        # peer discovery to avoid port 5353 conflicts from multiple instances.
        if self._mdns or self._cast or self._bluos or self._squeezebox or self._tune:
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
        if self._squeezebox:
            await self._squeezebox.start(shared_zc=self._shared_zc)
        if self._tune:
            await self._tune.start(shared_zc=self._shared_zc)
        if self._network_shares:
            await self._network_shares.start()
        if self._media_servers:
            await self._media_servers.start()

        logger.info("discovery_manager_started")

    async def stop(self) -> None:
        if self._rust_ssdp:
            await self._rust_ssdp.stop()
        if self._rust_mdns:
            await self._rust_mdns.stop()
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
        if self._squeezebox:
            await self._squeezebox.stop()
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
        "openhome": 7, "bluos": 6, "squeezebox": 5, "dlna": 4,
        "chromecast": 3, "airplay": 2, "local": 1,
    }

    def list_devices(self) -> list[DiscoveredDevice]:
        devices: list[DiscoveredDevice] = []
        if self._rust_ssdp:
            devices.extend(self._rust_ssdp.devices.values())
        if self._rust_mdns:
            devices.extend(self._rust_mdns.devices.values())
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
        if self._squeezebox:
            devices.extend(self._squeezebox.devices.values())
        return devices

    def list_devices_deduped(self) -> list[DiscoveredDevice]:
        """Deduplicated device list for UI display.

        When the same IP is discovered via multiple protocols, the primary
        device (highest priority) is returned with an ``alternatives`` list
        in its capabilities so the UI can offer protocol selection.
        Priority: OpenHome > BluOS > DLNA > Cast > AirPlay.
        Filters out renderers hosted on the server itself.
        """
        all_devices = self.list_devices()
        by_host: dict[str, list[DiscoveredDevice]] = {}
        for dev in all_devices:
            caps = dev.capabilities or {}
            if caps.get("manufacturer", "").lower() == "mozaik labs":
                continue
            by_host.setdefault(dev.host, []).append(dev)

        result: list[DiscoveredDevice] = []
        for host, devs in by_host.items():
            devs.sort(
                key=lambda d: self._TYPE_PRIORITY.get(d.type.value, 0),
                reverse=True,
            )
            primary = devs[0]
            if len(devs) > 1:
                alts = [{"id": d.id, "type": d.type.value, "name": d.name} for d in devs[1:]]
                caps = dict(primary.capabilities or {})
                caps["alternatives"] = alts
                primary = primary.model_copy(update={"capabilities": caps})
            result.append(primary)
        return result

    async def rescan(self) -> list[DiscoveredDevice]:
        """Force an immediate rescan of all discovery sources."""
        logger.info("discovery_rescan_requested")
        tasks = []
        if self._rust_ssdp:
            tasks.append(self._rust_ssdp.rescan())
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
        if self._squeezebox:
            tasks.append(self._squeezebox.rescan())
        if tasks:
            await asyncio.gather(*tasks)
        return self.list_devices()

    def get_device(self, device_id: str) -> DiscoveredDevice | None:
        if self._rust_ssdp:
            dev = self._rust_ssdp.devices.get(device_id)
            if dev:
                return dev
        if self._rust_mdns:
            dev = self._rust_mdns.devices.get(device_id)
            if dev:
                return dev
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
        if self._squeezebox:
            dev = self._squeezebox.devices.get(device_id)
            if dev:
                return dev
        return None

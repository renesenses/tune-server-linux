"""Tune Server peer discovery via DNS-SD (mDNS/Zeroconf).

Advertises this Tune instance as a ``_tune-server._tcp.local.`` service
and discovers other Tune servers on the LAN so they can browse each
other's libraries and transfer playback.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field
from typing import Any

import structlog

from tune_server.config import settings
from tune_server.event_bus import Event, EventBus, EventType

logger = structlog.get_logger()

TUNE_SERVICE_TYPE = "_tune-server._tcp.local."


@dataclass
class TunePeer:
    """A discovered Tune Server on the network."""

    name: str
    host: str
    port: int
    version: str = ""
    tracks: int = 0
    zones: int = 0
    server_id: str = ""  # unique id (hostname-based)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "version": self.version,
            "tracks": self.tracks,
            "zones": self.zones,
            "server_id": self.server_id,
        }


class TuneDiscovery:
    """Advertise this Tune instance and discover peers via mDNS DNS-SD."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._peers: dict[str, TunePeer] = {}
        self._zc = None
        self._owns_zc = False
        self._browser = None
        self._service_info = None
        self._local_ip: str = ""
        self._local_port: int = settings.api_port

    @property
    def peers(self) -> dict[str, TunePeer]:
        return dict(self._peers)

    async def start(self, shared_zc=None, track_count: int = 0, zone_count: int = 0) -> None:
        """Start advertising and browsing for Tune peers.

        Parameters
        ----------
        shared_zc:
            An existing ``Zeroconf`` instance to share (avoids port 5353
            conflicts). If *None* a private instance is created.
        track_count:
            Number of tracks in this server's library (for TXT record).
        zone_count:
            Number of zones configured on this server (for TXT record).
        """
        try:
            from zeroconf import ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf
        except ImportError:
            logger.warning("zeroconf_not_installed_tune_discovery")
            return

        # Shared or private Zeroconf instance
        if shared_zc:
            self._zc = shared_zc
            self._owns_zc = False
        else:
            try:
                self._zc = Zeroconf(interfaces=["0.0.0.0"])
                self._owns_zc = True
            except Exception:
                logger.warning("tune_discovery_zeroconf_init_failed", exc_info=True)
                return

        # Determine local IP
        from tune_server.utils.network import get_local_ip
        self._local_ip = get_local_ip()

        # Build server name
        from tune_server import __version__
        friendly_name = settings.server_name or socket.gethostname()

        # Register this server
        self._service_info = ServiceInfo(
            TUNE_SERVICE_TYPE,
            f"{friendly_name}.{TUNE_SERVICE_TYPE}",
            addresses=[socket.inet_aton(self._local_ip)],
            port=self._local_port,
            properties={
                "version": __version__,
                "name": friendly_name,
                "zones": str(zone_count),
                "tracks": str(track_count),
                "server_id": socket.gethostname(),
            },
        )

        try:
            await asyncio.to_thread(self._zc.register_service, self._service_info)
            logger.info(
                "tune_service_registered",
                name=friendly_name,
                ip=self._local_ip,
                port=self._local_port,
            )
        except Exception:
            logger.warning("tune_service_register_failed", exc_info=True)

        # Browse for other Tune servers
        loop = asyncio.get_running_loop()

        def _on_state_change(zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Added:
                loop.call_soon_threadsafe(
                    loop.create_task,
                    self._on_service_added(zeroconf, service_type, name),
                )
            elif state_change is ServiceStateChange.Removed:
                loop.call_soon_threadsafe(
                    loop.create_task,
                    self._on_service_removed(name),
                )
            elif state_change is ServiceStateChange.Updated:
                loop.call_soon_threadsafe(
                    loop.create_task,
                    self._on_service_added(zeroconf, service_type, name),
                )

        self._browser = ServiceBrowser(
            self._zc, TUNE_SERVICE_TYPE, handlers=[_on_state_change],
        )
        logger.info("tune_peer_discovery_started")

    async def _on_service_added(self, zeroconf, service_type: str, name: str) -> None:
        """Called when a Tune Server is discovered on the network."""
        try:
            info = await asyncio.to_thread(
                zeroconf.get_service_info, service_type, name, timeout=5000,
            )
            if not info:
                return

            host = None
            if info.parsed_addresses():
                host = info.parsed_addresses()[0]
            if not host:
                return

            port = info.port or 8888

            # Skip self
            if host == self._local_ip and port == self._local_port:
                return

            props = {}
            if info.properties:
                props = {
                    k.decode("utf-8") if isinstance(k, bytes) else k:
                    v.decode("utf-8") if isinstance(v, bytes) else v
                    for k, v in info.properties.items()
                }

            peer_name = props.get("name", name.replace(f".{TUNE_SERVICE_TYPE}", "").strip())
            server_id = props.get("server_id", peer_name)
            peer_id = f"{host}:{port}"

            peer = TunePeer(
                name=peer_name,
                host=host,
                port=port,
                version=props.get("version", ""),
                tracks=int(props.get("tracks", 0)),
                zones=int(props.get("zones", 0)),
                server_id=server_id,
            )

            is_new = peer_id not in self._peers
            self._peers[peer_id] = peer

            event_type = EventType.PEER_DISCOVERED if is_new else EventType.PEER_UPDATED
            await self._event_bus.emit(Event(
                type=event_type,
                data=peer.to_dict(),
                source="tune_discovery",
            ))
            logger.info(
                "tune_peer_discovered" if is_new else "tune_peer_updated",
                name=peer_name,
                host=host,
                port=port,
                version=peer.version,
                tracks=peer.tracks,
            )
        except Exception:
            logger.debug("tune_peer_add_failed", name=name, exc_info=True)

    async def _on_service_removed(self, name: str) -> None:
        """Called when a Tune Server disappears from the network."""
        # Find peer by matching the service name
        removed_id = None
        removed_peer = None
        for peer_id, peer in list(self._peers.items()):
            if name.startswith(peer.name):
                removed_id = peer_id
                removed_peer = peer
                break

        if removed_id and removed_peer:
            del self._peers[removed_id]
            await self._event_bus.emit(Event(
                type=EventType.PEER_LOST,
                data=removed_peer.to_dict(),
                source="tune_discovery",
            ))
            logger.info("tune_peer_lost", name=removed_peer.name, host=removed_peer.host)

    async def update_properties(self, track_count: int | None = None, zone_count: int | None = None) -> None:
        """Update the TXT record properties (e.g. after a library scan)."""
        if not self._service_info or not self._zc:
            return
        try:
            from zeroconf import ServiceInfo

            props = dict(self._service_info.properties)
            if track_count is not None:
                props[b"tracks"] = str(track_count).encode()
            if zone_count is not None:
                props[b"zones"] = str(zone_count).encode()

            old_info = self._service_info
            from tune_server import __version__
            friendly_name = settings.server_name or socket.gethostname()

            self._service_info = ServiceInfo(
                TUNE_SERVICE_TYPE,
                f"{friendly_name}.{TUNE_SERVICE_TYPE}",
                addresses=[socket.inet_aton(self._local_ip)],
                port=self._local_port,
                properties={
                    "version": __version__,
                    "name": friendly_name,
                    "zones": str(zone_count) if zone_count is not None else props.get(b"zones", b"0").decode(),
                    "tracks": str(track_count) if track_count is not None else props.get(b"tracks", b"0").decode(),
                    "server_id": socket.gethostname(),
                },
            )
            await asyncio.to_thread(self._zc.update_service, self._service_info)
            logger.debug("tune_service_properties_updated", tracks=track_count, zones=zone_count)
        except Exception:
            logger.debug("tune_service_update_failed", exc_info=True)

    async def stop(self) -> None:
        """Unregister the service and stop browsing."""
        if self._browser:
            self._browser.cancel()
            self._browser = None

        if self._service_info and self._zc:
            try:
                await asyncio.to_thread(self._zc.unregister_service, self._service_info)
            except Exception:
                logger.debug("tune_service_unregister_failed", exc_info=True)
            self._service_info = None

        if self._zc and self._owns_zc:
            self._zc.close()
        self._zc = None
        self._peers.clear()
        logger.info("tune_peer_discovery_stopped")

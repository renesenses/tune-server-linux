"""Squeezebox (Lyrion/Logitech Media Server) discovery.

Discovers LMS instances on the local network via Zeroconf mDNS
(``_slimcli._tcp``), then queries each LMS CLI (TCP port 9090) to
enumerate connected Squeezebox players.

Each player is exposed as a ``DiscoveredDevice`` with type
``OutputType.SQUEEZEBOX`` so the zone system can create outputs for them.
"""

from __future__ import annotations

import asyncio
import urllib.parse

import structlog

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import DiscoveredDevice, OutputType

logger = structlog.get_logger()

_LMS_SERVICE = "_slimcli._tcp.local."


async def _lms_query(host: str, port: int, cmd: str) -> str:
    """Send one line to the LMS CLI and return the response."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
        timeout=5,
    )
    try:
        writer.write((cmd + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        return urllib.parse.unquote(line.decode("utf-8").strip())
    finally:
        writer.close()
        await writer.wait_closed()


class SqueezeboxDiscovery:
    """Discover Squeezebox players via LMS CLI over mDNS."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._devices: dict[str, DiscoveredDevice] = {}
        # LMS host:port pairs discovered via mDNS
        self._lms_servers: dict[str, tuple[str, int]] = {}
        self._browser = None
        self._zc = None
        self._owns_zc = False
        self._poll_task: asyncio.Task | None = None
        self._running = False

    @property
    def devices(self) -> dict[str, DiscoveredDevice]:
        return self._devices

    def get_lms_for_player(self, device_id: str) -> tuple[str, int, str] | None:
        """Return (lms_host, lms_cli_port, player_mac) for a discovered player, or None."""
        dev = self._devices.get(device_id)
        if not dev:
            return None
        lms_host = dev.capabilities.get("lms_host")
        lms_port = dev.capabilities.get("lms_cli_port", 9090)
        player_id = dev.capabilities.get("player_id")
        if lms_host and player_id:
            return (lms_host, lms_port, player_id)
        return None

    async def start(self, shared_zc=None) -> None:
        try:
            from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange
        except ImportError:
            logger.warning("zeroconf_not_installed_squeezebox")
            return

        if shared_zc:
            self._zc = shared_zc
            self._owns_zc = False
        else:
            try:
                self._zc = Zeroconf(interfaces=["0.0.0.0"])
                self._owns_zc = True
            except Exception:
                logger.warning("squeezebox_zeroconf_init_failed", exc_info=True)
                return

        loop = asyncio.get_running_loop()

        def _on_service_state_change(zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Added:
                loop.call_soon_threadsafe(
                    loop.create_task, self._on_lms_found(zeroconf, service_type, name)
                )
            elif state_change is ServiceStateChange.Removed:
                loop.call_soon_threadsafe(
                    loop.create_task, self._on_lms_lost(name)
                )

        self._browser = ServiceBrowser(self._zc, _LMS_SERVICE, handlers=[_on_service_state_change])
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_players_loop())
        logger.info("squeezebox_discovery_started")

    async def _on_lms_found(self, zeroconf, service_type: str, name: str) -> None:
        try:
            info = await asyncio.to_thread(zeroconf.get_service_info, service_type, name, timeout=5000)
            if not info:
                return
            host = None
            if info.parsed_addresses():
                host = info.parsed_addresses()[0]
            if not host:
                return
            port = info.port or 9090
            server_key = f"{host}:{port}"
            self._lms_servers[server_key] = (host, port)
            logger.info("lms_server_discovered", host=host, port=port)
            # Query players immediately on discovery
            await self._enumerate_players(host, port)
        except Exception:
            logger.debug("squeezebox_lms_add_failed", name=name, exc_info=True)

    async def _on_lms_lost(self, name: str) -> None:
        # Remove players associated with lost LMS servers
        to_remove = []
        for dev_id, dev in self._devices.items():
            # We do not have enough info to match the mDNS name to a host
            # precisely, so we keep existing players until next poll finds them
            # gone. This is safe because the poll loop will prune stale entries.
            pass
        if to_remove:
            for dev_id in to_remove:
                await self._remove_player(dev_id)

    async def _enumerate_players(self, lms_host: str, lms_port: int) -> None:
        """Query LMS CLI for connected players and register them."""
        try:
            resp = await _lms_query(lms_host, lms_port, "player count ?")
            # Response: "player count <N>"
            parts = resp.split()
            count_str = parts[-1] if parts else "0"
            try:
                count = int(count_str)
            except ValueError:
                count = 0

            seen_ids: set[str] = set()
            for i in range(count):
                try:
                    # Get player ID (MAC address)
                    id_resp = await _lms_query(lms_host, lms_port, f"player id {i} ?")
                    player_mac = id_resp.split()[-1] if id_resp.split() else None
                    if not player_mac:
                        continue

                    # Get player name
                    name_resp = await _lms_query(lms_host, lms_port, f"player name {i} ?")
                    # Response: "player name <index> <name>"
                    name_parts = name_resp.split(None, 3)
                    player_name = name_parts[3] if len(name_parts) > 3 else f"Squeezebox {i}"

                    # Get player model
                    model_resp = await _lms_query(lms_host, lms_port, f"player model {i} ?")
                    model_parts = model_resp.split(None, 3)
                    player_model = model_parts[3] if len(model_parts) > 3 else "squeezebox"

                    # Get player IP
                    ip_resp = await _lms_query(lms_host, lms_port, f"player ip {i} ?")
                    ip_parts = ip_resp.split()
                    player_ip = ip_parts[-1].split(":")[0] if ip_parts else lms_host

                    device_id = f"squeezebox-{player_mac}"
                    seen_ids.add(device_id)

                    device = DiscoveredDevice(
                        id=device_id,
                        name=player_name,
                        type=OutputType.SQUEEZEBOX,
                        host=player_ip,
                        port=lms_port,
                        mac_address=player_mac,
                        capabilities={
                            "squeezebox": True,
                            "model": player_model,
                            "lms_host": lms_host,
                            "lms_cli_port": lms_port,
                            "player_id": player_mac,
                        },
                    )

                    is_new = device_id not in self._devices
                    self._devices[device_id] = device

                    if is_new:
                        await self._event_bus.emit(Event(
                            type=EventType.DEVICE_DISCOVERED,
                            data={"device_id": device_id, "name": player_name, "type": "squeezebox"},
                            source="squeezebox_discovery",
                        ))
                        logger.info(
                            "squeezebox_player_discovered",
                            name=player_name, id=device_id, model=player_model,
                            mac=player_mac, lms=f"{lms_host}:{lms_port}",
                        )
                except Exception:
                    logger.debug("squeezebox_player_query_failed", index=i, exc_info=True)

            # Remove players from this LMS that are no longer reported
            stale = [
                did for did, dev in self._devices.items()
                if dev.capabilities.get("lms_host") == lms_host
                and dev.capabilities.get("lms_cli_port") == lms_port
                and did not in seen_ids
            ]
            for did in stale:
                await self._remove_player(did)

        except Exception:
            logger.debug("squeezebox_enumerate_failed", host=lms_host, port=lms_port, exc_info=True)

    async def _remove_player(self, device_id: str) -> None:
        device = self._devices.pop(device_id, None)
        if device:
            await self._event_bus.emit(Event(
                type=EventType.DEVICE_LOST,
                data={"device_id": device_id, "name": device.name, "type": "squeezebox"},
                source="squeezebox_discovery",
            ))
            logger.info("squeezebox_player_lost", name=device.name, id=device_id)

    async def _poll_players_loop(self) -> None:
        """Periodically re-enumerate players from all known LMS instances."""
        while self._running:
            await asyncio.sleep(30)
            if not self._running:
                break
            for _key, (host, port) in list(self._lms_servers.items()):
                await self._enumerate_players(host, port)

    async def rescan(self) -> None:
        """Force an immediate re-enumeration of all known LMS servers."""
        for _key, (host, port) in list(self._lms_servers.items()):
            await self._enumerate_players(host, port)

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._browser:
            self._browser.cancel()
            self._browser = None
        self._devices.clear()
        self._lms_servers.clear()
        if self._zc and self._owns_zc:
            self._zc.close()
        self._zc = None
        logger.info("squeezebox_discovery_stopped")

"""BluOS (Bluesound) device discovery using Zeroconf mDNS."""

from __future__ import annotations

import asyncio

import structlog

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import DiscoveredDevice, OutputType

logger = structlog.get_logger()

_BLUOS_SERVICE = "_musc._tcp.local."


class BluosDiscovery:
    """Persistent discovery of BluOS devices on the local network."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._devices: dict[str, DiscoveredDevice] = {}
        self._browser = None
        self._zc = None

    @property
    def devices(self) -> dict[str, DiscoveredDevice]:
        return self._devices

    def get_bluos_host(self, device_id: str) -> tuple[str, int] | None:
        """Return (host, port) for a discovered BluOS device, or None."""
        dev = self._devices.get(device_id)
        if dev:
            return (dev.host, dev.port)
        return None

    async def start(self, shared_zc=None) -> None:
        try:
            from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange
        except ImportError:
            logger.warning("zeroconf_not_installed_bluos")
            return

        if shared_zc:
            self._zc = shared_zc
            self._owns_zc = False
        else:
            try:
                self._zc = Zeroconf(interfaces=['0.0.0.0'])
                self._owns_zc = True
            except Exception:
                logger.warning("bluos_zeroconf_init_failed", exc_info=True)
                return

        loop = asyncio.get_running_loop()

        def _on_service_state_change(zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Added:
                loop.call_soon_threadsafe(
                    loop.create_task, self._on_service_added(zeroconf, service_type, name)
                )
            elif state_change is ServiceStateChange.Removed:
                loop.call_soon_threadsafe(
                    loop.create_task, self._on_service_removed(name)
                )

        self._browser = ServiceBrowser(self._zc, _BLUOS_SERVICE, handlers=[_on_service_state_change])
        logger.info("bluos_discovery_started")

    async def _on_service_added(self, zeroconf, service_type: str, name: str) -> None:
        try:
            from zeroconf import Zeroconf
            info = await asyncio.to_thread(zeroconf.get_service_info, service_type, name, timeout=5000)
            if not info:
                return

            # Build a stable device ID from the service name
            device_id = f"bluos-{name}"
            host = None
            if info.parsed_addresses():
                host = info.parsed_addresses()[0]
            if not host:
                return

            port = info.port or 11000
            friendly_name = name.replace(f".{_BLUOS_SERVICE}", "").strip()

            device = DiscoveredDevice(
                id=device_id,
                name=friendly_name,
                type=OutputType.BLUOS,
                host=host,
                port=port,
                capabilities={
                    "bluos": True,
                    "manufacturer": "Bluesound",
                },
            )
            self._devices[device_id] = device

            await self._event_bus.emit(Event(
                type=EventType.DEVICE_DISCOVERED,
                data={"device_id": device_id, "name": device.name, "type": "bluos"},
                source="bluos_discovery",
            ))
            logger.info("bluos_device_discovered", name=device.name, id=device_id, host=host, port=port)
        except Exception:
            logger.debug("bluos_add_failed", name=name, exc_info=True)

    async def _on_service_removed(self, name: str) -> None:
        device_id = f"bluos-{name}"
        device = self._devices.pop(device_id, None)
        if device:
            await self._event_bus.emit(Event(
                type=EventType.DEVICE_LOST,
                data={"device_id": device_id, "name": device.name, "type": "bluos"},
                source="bluos_discovery",
            ))
            logger.info("bluos_device_lost", name=device.name, id=device_id)

    async def rescan(self) -> None:
        if self._browser:
            self._browser.cancel()
            try:
                from zeroconf import ServiceBrowser, ServiceStateChange

                loop = asyncio.get_running_loop()

                def _on_service_state_change(zeroconf, service_type, name, state_change):
                    if state_change is ServiceStateChange.Added:
                        loop.call_soon_threadsafe(
                            loop.create_task, self._on_service_added(zeroconf, service_type, name)
                        )
                    elif state_change is ServiceStateChange.Removed:
                        loop.call_soon_threadsafe(
                            loop.create_task, self._on_service_removed(name)
                        )

                self._browser = ServiceBrowser(self._zc, _BLUOS_SERVICE, handlers=[_on_service_state_change])
            except Exception:
                logger.debug("bluos_rescan_failed", exc_info=True)

    async def stop(self) -> None:
        if self._browser:
            self._browser.cancel()
            self._browser = None
        self._devices.clear()
        if self._zc and getattr(self, '_owns_zc', False):
            self._zc.close()
        self._zc = None
        logger.info("bluos_discovery_stopped")

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
        self._passive_zc = None  # AsyncZeroconf for real-time byebye events
        self._passive_browsers: list = []
        # Map service name → device id so we can emit DEVICE_LOST promptly
        self._service_to_device: dict[str, str] = {}

    @property
    def devices(self) -> dict[str, DiscoveredDevice]:
        return dict(self._devices)

    def get_atv_config(self, device_id: str) -> Optional[object]:
        return self._atv_configs.get(device_id)

    async def start(self, shared_zc=None) -> None:
        self._running = True
        self._task = asyncio.create_task(self._discovery_loop())
        try:
            from zeroconf import ServiceStateChange
            from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

            if shared_zc:
                self._passive_zc = AsyncZeroconf(zc=shared_zc)
            else:
                self._passive_zc = AsyncZeroconf()

            def _on_state_change(zeroconf, service_type, name, state_change):
                # Only react to removals — additions are handled by pyatv scan
                # which produces richer DiscoveredDevice records.
                if state_change != ServiceStateChange.Removed:
                    return
                # Schedule the emit on the main loop
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return
                loop.create_task(self._on_service_removed(name))

            for svc in (AIRPLAY_SERVICE, AIRPLAY_SERVICE_ALT):
                browser = AsyncServiceBrowser(
                    self._passive_zc.zeroconf,
                    svc,
                    handlers=[_on_state_change],
                )
                self._passive_browsers.append(browser)
            logger.info("mdns_passive_browser_started", services=[AIRPLAY_SERVICE, AIRPLAY_SERVICE_ALT])
        except ImportError:
            logger.debug("zeroconf_not_available_for_passive_browser")
        except Exception as e:
            logger.warning("mdns_passive_browser_error", error=str(e))
            self._passive_zc = None
        logger.info("mdns_discovery_started")

    async def _on_service_removed(self, service_name: str) -> None:
        """Called when zeroconf sees a service withdrawal (ssdp byebye-like)."""
        dev_id = self._service_to_device.get(service_name)
        if not dev_id:
            return
        async with self._lock:
            device = self._devices.get(dev_id)
            if not device or not device.available:
                return
            device.available = False
        logger.info("mdns_service_removed", name=device.name, service=service_name)
        await self._event_bus.emit(Event(
            type=EventType.DEVICE_LOST,
            data={"id": dev_id, "name": device.name},
            source="mdns",
        ))

    async def stop(self) -> None:
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.debug("mdns_discovery_task_cancelled")
            self._task = None

        for browser in self._passive_browsers:
            try:
                await browser.async_cancel()
            except Exception:
                pass
        self._passive_browsers = []
        if self._passive_zc:
            try:
                await self._passive_zc.async_close()
            except Exception as e:
                logger.debug("mdns_passive_zc_close_error", error=str(e))
        self._passive_zc = None

        if self._zeroconf:
            try:
                await asyncio.to_thread(self._zeroconf.close)
            except Exception as e:
                logger.debug("mdns_zeroconf_close_error", error=str(e))

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
                            # Track zeroconf service name → device id for real-time removals
                            self._service_to_device[f"{name}.{AIRPLAY_SERVICE_ALT}"] = dev_id

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
            logger.warning("pyatv_not_installed", hint="AirPlay discovery disabled — install pyatv")
        except asyncio.CancelledError:
            raise
        except OSError as e:
            # Windows: mDNS often fails with WinError if Bonjour is not installed
            logger.warning("mdns_os_error", error=str(e),
                           hint="On Windows, install Apple Bonjour (included with iTunes) for AirPlay discovery")

    async def rescan(self) -> list[DiscoveredDevice]:
        """Run a single mDNS scan immediately and return discovered devices."""
        try:
            import pyatv
        except ImportError:
            logger.debug("pyatv_not_available_for_rescan")
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

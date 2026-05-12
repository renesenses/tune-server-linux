"""Google Cast device discovery using pychromecast."""

from __future__ import annotations

import asyncio

import structlog

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import DiscoveredDevice, OutputType

logger = structlog.get_logger()


class CastDiscovery:
    """Persistent discovery of Google Cast devices on the local network."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._devices: dict[str, DiscoveredDevice] = {}
        self._cast_devices: dict[str, object] = {}
        self._browser = None
        self._zc = None

    @property
    def devices(self) -> dict[str, DiscoveredDevice]:
        return self._devices

    def get_cast_device(self, device_id: str):
        return self._cast_devices.get(device_id)

    async def start(self, shared_zc=None) -> None:
        try:
            import pychromecast
            from pychromecast.discovery import CastBrowser, SimpleCastListener
        except ImportError:
            logger.warning("pychromecast_not_installed")
            return

        if shared_zc:
            self._zc = shared_zc
            self._owns_zc = False
        else:
            try:
                from zeroconf import Zeroconf
                self._zc = Zeroconf()
                self._owns_zc = True
            except Exception:
                logger.warning("cast_zeroconf_init_failed")
                return

        listener = SimpleCastListener(
            add_callback=lambda uuid, name: asyncio.get_event_loop().call_soon_threadsafe(
                asyncio.ensure_future, self._on_cast_added(uuid, name)
            ),
            remove_callback=lambda uuid, name, _: asyncio.get_event_loop().call_soon_threadsafe(
                asyncio.ensure_future, self._on_cast_removed(uuid)
            ),
        )
        self._browser = CastBrowser(listener, self._zc)
        self._browser.start_discovery()
        logger.info("cast_discovery_started")

    async def _on_cast_added(self, uuid, name: str) -> None:
        try:
            import pychromecast
            casts, _ = pychromecast.get_listed_chromecasts(
                friendly_names=[name], zeroconf_instance=self._zc
            )
            if not casts:
                return
            cast = casts[0]
            cast.wait(timeout=10)
            device_id = str(uuid)
            self._cast_devices[device_id] = cast

            info = cast.cast_info
            device = DiscoveredDevice(
                id=device_id,
                name=info.friendly_name or name,
                type=OutputType.CHROMECAST,
                host=str(info.host),
                port=info.port,
                capabilities={
                    "chromecast": True,
                    "manufacturer": info.manufacturer or "Google",
                    "model": info.model_name or "",
                    "cast_type": cast.cast_type or "cast",
                },
            )
            self._devices[device_id] = device

            await self._event_bus.emit(Event(
                type=EventType.DEVICE_DISCOVERED,
                data={"device_id": device_id, "name": device.name, "type": "chromecast"},
                source="cast_discovery",
            ))
            logger.info("cast_device_discovered", name=device.name, id=device_id, model=info.model_name)
        except Exception:
            logger.debug("cast_add_failed", name=name, exc_info=True)

    async def _on_cast_removed(self, uuid) -> None:
        device_id = str(uuid)
        device = self._devices.pop(device_id, None)
        self._cast_devices.pop(device_id, None)
        if device:
            await self._event_bus.emit(Event(
                type=EventType.DEVICE_LOST,
                data={"device_id": device_id, "name": device.name, "type": "chromecast"},
                source="cast_discovery",
            ))
            logger.info("cast_device_lost", name=device.name, id=device_id)

    async def rescan(self) -> None:
        if self._browser:
            self._browser.stop_discovery()
            self._browser.start_discovery()

    async def stop(self) -> None:
        if self._browser:
            self._browser.stop_discovery()
            self._browser = None
        for cast in self._cast_devices.values():
            try:
                cast.disconnect()
            except Exception:
                pass
        self._cast_devices.clear()
        self._devices.clear()
        if self._zc and getattr(self, '_owns_zc', False):
            self._zc.close()
        self._zc = None
        logger.info("cast_discovery_stopped")

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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pychromecast = None

    @property
    def devices(self) -> dict[str, DiscoveredDevice]:
        return self._devices

    def get_cast_device(self, device_id: str):
        return self._cast_devices.get(device_id)

    async def reconnect_cast_device(self, device_id: str):
        """Create a fresh pychromecast connection, replacing any stale one."""
        device = self._devices.get(device_id)
        if not device or not self._pychromecast:
            return None
        old = self._cast_devices.pop(device_id, None)
        if old:
            try:
                old.disconnect()
            except Exception:
                pass

        def _connect():
            try:
                result, _ = self._pychromecast.get_listed_chromecasts(
                    friendly_names=[device.name], zeroconf_instance=self._zc
                )
                if result:
                    cc = result[0]
                    cc.wait(timeout=10)
                    return cc
            except Exception:
                logger.debug("cast_reconnect_failed", name=device.name)
            return None

        cast = await asyncio.to_thread(_connect)
        if cast:
            self._cast_devices[device_id] = cast
        return cast

    async def start(self, shared_zc=None) -> None:
        try:
            import pychromecast
            from pychromecast.discovery import CastBrowser, SimpleCastListener
        except ImportError:
            logger.warning("pychromecast_not_installed")
            return

        self._pychromecast = pychromecast
        self._loop = asyncio.get_running_loop()

        if shared_zc:
            self._zc = shared_zc
            self._owns_zc = False
        else:
            try:
                from zeroconf import Zeroconf
                self._zc = Zeroconf(interfaces=['0.0.0.0'])
                self._owns_zc = True
            except Exception:
                logger.warning("cast_zeroconf_init_failed", exc_info=True)
                return

        def _on_add(uuid, name):
            self._loop.call_soon_threadsafe(self._schedule_add, uuid, name)

        def _on_remove(uuid, name, _service):
            self._loop.call_soon_threadsafe(self._schedule_remove, uuid)

        listener = SimpleCastListener(add_callback=_on_add, remove_callback=_on_remove)
        self._browser = CastBrowser(listener, self._zc)
        self._browser.start_discovery()
        logger.info("cast_discovery_started")

    def _schedule_add(self, uuid, name: str) -> None:
        self._loop.create_task(self._on_cast_added(uuid, name))

    def _schedule_remove(self, uuid) -> None:
        self._loop.create_task(self._on_cast_removed(uuid))

    async def _on_cast_added(self, uuid, name: str) -> None:
        try:
            device_id = str(uuid)
            if device_id in self._devices:
                return

            friendly = name
            host = ""
            port = 8009
            manufacturer = "Google"
            model = ""
            cast_type = "cast"

            if self._browser and hasattr(self._browser, 'services'):
                cast_info = self._browser.services.get(uuid)
                if cast_info:
                    host = str(getattr(cast_info, 'host', "")) or ""
                    port = getattr(cast_info, 'port', 8009) or 8009
                    friendly = getattr(cast_info, 'friendly_name', name) or name
                    manufacturer = getattr(cast_info, 'manufacturer', "Google") or "Google"
                    model = getattr(cast_info, 'model_name', "") or ""
                    cast_type = getattr(cast_info, 'cast_type', "cast") or "cast"

            device = DiscoveredDevice(
                id=device_id,
                name=friendly,
                type=OutputType.CHROMECAST,
                host=host,
                port=port,
                capabilities={
                    "chromecast": True,
                    "manufacturer": manufacturer,
                    "model": model,
                    "cast_type": cast_type,
                },
            )
            self._devices[device_id] = device

            def _connect():
                try:
                    result, _ = self._pychromecast.get_listed_chromecasts(
                        friendly_names=[friendly], zeroconf_instance=self._zc
                    )
                    if result:
                        cc = result[0]
                        cc.wait(timeout=10)
                        return cc
                except Exception:
                    logger.debug("cast_connect_failed", name=friendly)
                return None

            cast = await asyncio.to_thread(_connect)
            if cast:
                self._cast_devices[device_id] = cast

            await self._event_bus.emit(Event(
                type=EventType.DEVICE_DISCOVERED,
                data={"device_id": device_id, "name": device.name, "type": "chromecast"},
                source="cast_discovery",
            ))
            logger.info("cast_device_discovered", name=device.name, id=device_id,
                        host=host, model=model, cast_type=cast_type,
                        connected=cast is not None)
        except Exception:
            logger.exception("cast_add_failed", name=name)

    async def _on_cast_removed(self, uuid) -> None:
        device_id = str(uuid)
        device = self._devices.pop(device_id, None)
        cc = self._cast_devices.pop(device_id, None)
        if cc:
            try:
                cc.disconnect()
            except Exception:
                pass
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

"""Rust-accelerated discovery backend (Phase 3 migration).

When tune_native is available and TUNE_DISCOVERY_ENGINE != "python",
this module provides SSDP and mDNS scanning via Rust for faster
network discovery (10x faster XML parsing, native multicast sockets).

The Python DiscoveryManager keeps DmrDevice creation and output control;
the Rust layer only handles the network scanning and device identification.
"""
from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import structlog

from tune_server.models import DiscoveredDevice, OutputType

if TYPE_CHECKING:
    from tune_server.event_bus import EventBus

logger = structlog.get_logger()

try:
    import tune_native

    _RUST_AVAILABLE = hasattr(tune_native, "RustSsdpScanner")
except ImportError:
    _RUST_AVAILABLE = False


def rust_discovery_available() -> bool:
    engine = os.environ.get("TUNE_DISCOVERY_ENGINE", "auto")
    return _RUST_AVAILABLE and engine != "python"


_TYPE_MAP = {
    "dlna": OutputType.DLNA,
    "airplay": OutputType.AIRPLAY,
    "chromecast": OutputType.CHROMECAST,
    "bluos": OutputType.BLUOS,
    "openhome": OutputType.OPENHOME,
    "squeezebox": OutputType.SQUEEZEBOX,
    "local": OutputType.LOCAL,
}


def _rust_device_to_model(d: dict) -> DiscoveredDevice:
    """Convert Rust scanner device dict to Python DiscoveredDevice."""
    return DiscoveredDevice(
        id=d["id"],
        name=d["name"],
        type=_TYPE_MAP.get(d.get("type", "dlna"), OutputType.DLNA),
        host=d.get("host", ""),
        port=d.get("port", 0),
        available=d.get("available", True),
        capabilities=d.get("capabilities", {}),
        manufacturer=d.get("manufacturer"),
        model=d.get("model"),
        airplay_version=d.get("airplay_version"),
        mac_address=d.get("mac_address"),
    )


class RustSsdpBackend:
    """Wraps tune_native.RustSsdpScanner for use by DiscoveryManager.

    Maintains a sync-accessible `devices` dict (like Python SsdpDiscovery)
    populated by a background event polling task.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._scanner = tune_native.RustSsdpScanner()
        self._event_task: asyncio.Task | None = None
        self.devices: dict[str, DiscoveredDevice] = {}

    async def start(self) -> None:
        await asyncio.to_thread(self._scanner.start)
        self._event_task = asyncio.create_task(self._poll_events())
        logger.info("rust_ssdp_backend_started")

    async def stop(self) -> None:
        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self._scanner.stop)
        logger.info("rust_ssdp_backend_stopped")

    async def rescan(self) -> list[DiscoveredDevice]:
        raw = await asyncio.to_thread(self._scanner.rescan)
        devices = [_rust_device_to_model(d) for d in raw]
        for dev in devices:
            self.devices[dev.id] = dev
        return devices

    async def _poll_events(self) -> None:
        from tune_server.event_bus import Event, EventType

        while True:
            try:
                event = await asyncio.to_thread(self._scanner.poll_event, 1000)
                if event is None:
                    await asyncio.sleep(0.1)
                    continue
                if event["event"] == "discovered":
                    device = _rust_device_to_model(event["device"])
                    self.devices[device.id] = device
                    await self._event_bus.emit(Event(
                        type=EventType.DEVICE_DISCOVERED,
                        data={"device": device.model_dump()},
                    ))
                elif event["event"] == "lost":
                    dev_id = event["device_id"]
                    self.devices.pop(dev_id, None)
                    await self._event_bus.emit(Event(
                        type=EventType.DEVICE_LOST,
                        data={"device_id": dev_id},
                    ))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("rust_ssdp_poll_error")
                await asyncio.sleep(1)


class RustMdnsBackend:
    """Wraps tune_native.RustMdnsScanner for use by DiscoveryManager.

    Maintains a sync-accessible `devices` dict populated by event polling.
    """

    def __init__(
        self,
        event_bus: EventBus,
        airplay: bool = True,
        bluos: bool = True,
        chromecast: bool = True,
        squeezebox: bool = True,
        tune_peers: bool = True,
    ) -> None:
        self._event_bus = event_bus
        self._scanner = tune_native.RustMdnsScanner(
            airplay=airplay,
            bluos=bluos,
            chromecast=chromecast,
            squeezebox=squeezebox,
            tune_peers=tune_peers,
        )
        self._event_task: asyncio.Task | None = None
        self.devices: dict[str, DiscoveredDevice] = {}

    async def start(self) -> None:
        await asyncio.to_thread(self._scanner.start)
        self._event_task = asyncio.create_task(self._poll_events())
        logger.info("rust_mdns_backend_started")

    async def stop(self) -> None:
        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self._scanner.stop)
        logger.info("rust_mdns_backend_stopped")

    async def _poll_events(self) -> None:
        from tune_server.event_bus import Event, EventType

        while True:
            try:
                event = await asyncio.to_thread(self._scanner.poll_event, 1000)
                if event is None:
                    await asyncio.sleep(0.1)
                    continue
                if event["event"] == "discovered":
                    device = _rust_device_to_model(event["device"])
                    self.devices[device.id] = device
                    await self._event_bus.emit(Event(
                        type=EventType.DEVICE_DISCOVERED,
                        data={"device": device.model_dump()},
                    ))
                elif event["event"] == "lost":
                    dev_id = event["device_id"]
                    self.devices.pop(dev_id, None)
                    await self._event_bus.emit(Event(
                        type=EventType.DEVICE_LOST,
                        data={"device_id": dev_id},
                    ))
                elif event["event"] == "updated":
                    device = _rust_device_to_model(event["device"])
                    self.devices[device.id] = device
                    await self._event_bus.emit(Event(
                        type=EventType.DEVICE_DISCOVERED,
                        data={"device": device.model_dump()},
                    ))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("rust_mdns_poll_error")
                await asyncio.sleep(1)

"""OpenHome device discovery via SSDP.

Searches for devices advertising the OpenHome Product service
(urn:av-openhome-org:service:Product:1). These are hi-fi streamers
from Linn, Naim, Auralic, Lumin, dCS, Cambridge Audio, etc.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse
from xml.etree import ElementTree

import aiohttp
import structlog

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import DiscoveredDevice, OutputType

logger = structlog.get_logger()

OH_PRODUCT_URN = "urn:av-openhome-org:service:Product:1"
OH_PLAYLIST_URN = "urn:av-openhome-org:service:Playlist:1"
OH_TRANSPORT_URN = "urn:av-openhome-org:service:Transport:1"
OH_VOLUME_URN = "urn:av-openhome-org:service:Volume:1"
OH_INFO_URN = "urn:av-openhome-org:service:Info:1"
OH_TIME_URN = "urn:av-openhome-org:service:Time:1"

_NS = {"upnp": "urn:schemas-upnp-org:device-1-0"}


class OpenHomeDiscovery:
    """Discover OpenHome devices on the local network via SSDP."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._devices: dict[str, DiscoveredDevice] = {}
        self._service_urls: dict[str, dict[str, str]] = {}
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def devices(self) -> dict[str, DiscoveredDevice]:
        return self._devices

    def get_service_urls(self, device_id: str) -> dict[str, str] | None:
        return self._service_urls.get(device_id)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._search_loop())
        logger.info("openhome_discovery_started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def rescan(self) -> None:
        await self._ssdp_search()

    async def _search_loop(self) -> None:
        while self._running:
            try:
                await self._ssdp_search()
            except Exception:
                logger.debug("openhome_search_error", exc_info=True)
            await asyncio.sleep(60)

    async def _ssdp_search(self) -> None:
        """Send M-SEARCH for OpenHome Product service."""
        import socket

        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 3\r\n"
            f"ST: {OH_PRODUCT_URN}\r\n"
            "\r\n"
        ).encode()

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(4)
        sock.sendto(msg, ("239.255.255.250", 1900))

        loop = asyncio.get_event_loop()
        responses: list[bytes] = []

        def _recv():
            while True:
                try:
                    data, _ = sock.recvfrom(4096)
                    responses.append(data)
                except socket.timeout:
                    break
            sock.close()

        await loop.run_in_executor(None, _recv)

        for data in responses:
            try:
                text = data.decode(errors="replace")
                location = None
                usn = None
                for line in text.split("\r\n"):
                    lower = line.lower()
                    if lower.startswith("location:"):
                        location = line.split(":", 1)[1].strip()
                    elif lower.startswith("usn:"):
                        usn = line.split(":", 1)[1].strip()
                if location and usn:
                    dev_id = usn.split("::")[0] if "::" in usn else usn
                    if dev_id not in self._devices:
                        await self._probe_device(dev_id, location)
            except Exception:
                logger.debug("openhome_response_parse_error", exc_info=True)

    async def _probe_device(self, dev_id: str, location: str) -> None:
        """Fetch the device description XML and extract OpenHome service URLs."""
        parsed = urlparse(location)
        host = parsed.hostname or ""
        port = parsed.port or 80
        base_url = f"http://{host}:{port}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(location, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return
                    xml_text = await resp.text()
        except Exception as exc:
            logger.debug("openhome_probe_fetch_error", location=location, error=str(exc))
            return

        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError:
            return

        friendly_name = ""
        model = ""
        manufacturer = ""

        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "friendlyName" and not friendly_name:
                friendly_name = (elem.text or "").strip()
            elif tag == "modelName" and not model:
                model = (elem.text or "").strip()
            elif tag == "manufacturer" and not manufacturer:
                manufacturer = (elem.text or "").strip()

        if not friendly_name:
            friendly_name = model or "OpenHome Device"

        # Skip if already discovered as DLNA (check SSDP devices)
        # OpenHome devices often also advertise as DLNA — we prefer OpenHome
        manufacturer_lower = (manufacturer or "").lower()
        if "google" in manufacturer_lower:
            return

        # Extract service control URLs
        services: dict[str, str] = {}
        for service_elem in root.iter():
            stag = service_elem.tag.split("}")[-1] if "}" in service_elem.tag else service_elem.tag
            if stag == "service":
                stype = ""
                control_url = ""
                for child in service_elem:
                    ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if ctag == "serviceType":
                        stype = (child.text or "").strip()
                    elif ctag == "controlURL":
                        control_url = (child.text or "").strip()
                if stype and control_url:
                    if not control_url.startswith("http"):
                        control_url = base_url + ("" if control_url.startswith("/") else "/") + control_url
                    if "Product" in stype:
                        services["product"] = control_url
                    elif "Playlist" in stype:
                        services["playlist"] = control_url
                    elif "Transport" in stype:
                        services["transport"] = control_url
                    elif "Volume" in stype:
                        services["volume"] = control_url
                    elif "Info" in stype:
                        services["info"] = control_url
                    elif "Time" in stype:
                        services["time"] = control_url

        if "playlist" not in services:
            logger.debug("openhome_no_playlist_service", name=friendly_name, services=list(services.keys()))
            return

        device = DiscoveredDevice(
            id=dev_id,
            name=friendly_name,
            type=OutputType.OPENHOME,
            host=host,
            port=port,
            capabilities={
                "manufacturer": manufacturer,
                "model": model,
                "services": list(services.keys()),
                "base_url": base_url,
            },
        )

        self._devices[dev_id] = device
        self._service_urls[dev_id] = services

        logger.info("openhome_device_found",
                     name=friendly_name, manufacturer=manufacturer,
                     model=model, host=host, services=list(services.keys()))

        await self._event_bus.emit(Event(
            type=EventType.DEVICE_DISCOVERED,
            data={"device_id": dev_id, "name": friendly_name, "type": "openhome"},
        ))

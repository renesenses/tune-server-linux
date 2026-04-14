"""SSDP discovery for DLNA/UPnP Media Servers (ContentDirectory).

Uses raw socket SSDP M-SEARCH for reliability instead of async_upnp_client.search
which has task lifecycle issues. Device descriptions are fetched via aiohttp with
proper timeouts.
"""
from __future__ import annotations

import asyncio
import socket
import struct
from typing import Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import structlog

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import DiscoveredMediaServer

logger = structlog.get_logger()

MEDIA_SERVER_URN = "urn:schemas-upnp-org:device:MediaServer:1"
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_MX = 5  # Max wait time in seconds
SCAN_INTERVAL = 60  # Seconds between scans

MSEARCH_MSG = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
    'MAN: "ssdp:discover"\r\n'
    f"MX: {SSDP_MX}\r\n"
    f"ST: {MEDIA_SERVER_URN}\r\n"
    "\r\n"
)

# UPnP XML namespaces
NS = {
    "d": "urn:schemas-upnp-org:device-1-0",
}


class MediaServerDiscovery:
    """SSDP discovery for DLNA/UPnP Media Servers."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._servers: dict[str, DiscoveredMediaServer] = {}
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def servers(self) -> dict[str, DiscoveredMediaServer]:
        return dict(self._servers)

    def get_upnp_device(self, server_id: str) -> Optional[object]:
        """Kept for API compatibility. Returns None (no upnp_client objects)."""
        return None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._discovery_loop())
        logger.info("media_server_discovery_started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("media_server_discovery_stopped")

    async def _discovery_loop(self) -> None:
        """Main loop: SSDP search → fetch device descriptions → update registry."""
        # Small delay to let the server finish booting
        await asyncio.sleep(5)

        while self._running:
            try:
                await self._scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("media_server_scan_error")

            try:
                await asyncio.sleep(SCAN_INTERVAL)
            except asyncio.CancelledError:
                raise

    async def _scan(self) -> None:
        """Perform one SSDP search cycle."""
        # 1. Send M-SEARCH and collect responses
        responses = await self._ssdp_search()

        # 2. Process each response
        discovered_ids: set[str] = set()

        for location, usn in responses:
            if not location:
                continue

            dev_id = usn or location

            if dev_id in discovered_ids:
                continue
            discovered_ids.add(dev_id)

            # Skip if already known and available
            if dev_id in self._servers and self._servers[dev_id].available:
                continue

            # Fetch device description
            info = await self._fetch_device_info(location)
            if not info:
                continue

            name, manufacturer, model, host, port = info

            server = DiscoveredMediaServer(
                id=dev_id,
                name=name,
                host=host,
                port=port,
                manufacturer=manufacturer,
                model=model,
                available=True,
            )

            was_lost = dev_id in self._servers and not self._servers[dev_id].available
            is_new = dev_id not in self._servers
            self._servers[dev_id] = server

            if is_new or was_lost:
                await self._event_bus.emit(Event(
                    type=EventType.MEDIA_SERVER_DISCOVERED,
                    data=server.model_dump(),
                    source="media_server_discovery",
                ))
                logger.info(
                    "media_server_found",
                    name=name,
                    manufacturer=manufacturer,
                    model=model,
                    host=host,
                    id=dev_id,
                    recovered=was_lost,
                )

        # 3. Mark lost servers
        for dev_id in list(self._servers.keys()):
            if dev_id not in discovered_ids:
                server = self._servers[dev_id]
                if server.available:
                    server.available = False
                    await self._event_bus.emit(Event(
                        type=EventType.MEDIA_SERVER_LOST,
                        data={"id": dev_id, "name": server.name},
                        source="media_server_discovery",
                    ))
                    logger.info("media_server_lost", name=server.name, id=dev_id)

    async def _ssdp_search(self) -> list[tuple[str, str]]:
        """Send SSDP M-SEARCH and collect (location, usn) tuples."""
        results: list[tuple[str, str]] = []

        try:
            loop = asyncio.get_running_loop()

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setblocking(False)
            sock.settimeout(0)

            # Send M-SEARCH
            await loop.sock_sendto(sock, MSEARCH_MSG.encode(), (SSDP_ADDR, SSDP_PORT))

            # Collect responses
            deadline = loop.time() + SSDP_MX + 1
            while loop.time() < deadline:
                try:
                    data = await asyncio.wait_for(
                        loop.sock_recv(sock, 4096),
                        timeout=max(0.1, deadline - loop.time()),
                    )
                    text = data.decode("utf-8", errors="replace")
                    headers = _parse_ssdp_response(text)
                    location = headers.get("location", "")
                    usn = headers.get("usn", "")
                    st = headers.get("st", "")

                    if MEDIA_SERVER_URN in st or MEDIA_SERVER_URN in usn:
                        results.append((location, usn))

                except asyncio.TimeoutError:
                    break
                except OSError:
                    break

            sock.close()

        except Exception as e:
            logger.debug("ssdp_search_error", error=str(e))

        return results

    async def _fetch_device_info(
        self, location: str
    ) -> Optional[tuple[str, str | None, str | None, str, int]]:
        """Fetch and parse UPnP device description XML.

        Returns (name, manufacturer, model, host, port) or None on error.
        """
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(location, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return None
                    xml_text = await resp.text()

            root = ET.fromstring(xml_text)
            device_el = root.find(".//d:device", NS)
            if device_el is None:
                return None

            name = _xml_text(device_el, "d:friendlyName", NS) or "Unknown MediaServer"
            manufacturer = _xml_text(device_el, "d:manufacturer", NS)
            model = _xml_text(device_el, "d:modelName", NS)

            parsed = urlparse(location)
            host = parsed.hostname or ""
            port = parsed.port or 0

            return (name, manufacturer, model, host, port)

        except Exception as e:
            logger.debug("device_fetch_error", location=location, error=str(e))
            return None


def _parse_ssdp_response(text: str) -> dict[str, str]:
    """Parse SSDP response headers into a dict (lowercase keys)."""
    headers: dict[str, str] = {}
    for line in text.split("\r\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    return headers


def _xml_text(parent: ET.Element, tag: str, ns: dict) -> str | None:
    """Extract text from an XML element, or None."""
    el = parent.find(tag, ns)
    return el.text.strip() if el is not None and el.text else None

"""Discover Tune Servers on the LAN via SSDP + device.xml probe."""
from __future__ import annotations

import asyncio
import socket
import struct
from dataclasses import dataclass

import aiohttp
import structlog

logger = structlog.get_logger()

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900


@dataclass
class TuneServerInfo:
    name: str
    host: str
    port: int
    uuid: str

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


async def discover_tune_servers(timeout: float = 5.0) -> list[TuneServerInfo]:
    """Discover Tune Servers on the LAN via SSDP M-SEARCH.

    Returns list of discovered Tune Server instances.
    """
    locations: set[str] = set()
    servers: list[TuneServerInfo] = []

    # Send M-SEARCH for MediaServer devices
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 3\r\n"
        "ST: urn:schemas-upnp-org:device:MediaServer:1\r\n"
        "\r\n"
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    sock.bind(("", 0))

    try:
        sock.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))

        end_time = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < end_time:
            try:
                data, addr = sock.recvfrom(4096)
                response = data.decode("utf-8", errors="ignore")
                for line in response.split("\r\n"):
                    if line.upper().startswith("LOCATION:"):
                        locations.add(line.split(":", 1)[1].strip())
            except socket.timeout:
                break
    finally:
        sock.close()

    # Probe each location for Tune Server device.xml
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
        for loc in locations:
            try:
                async with session.get(loc) as resp:
                    if resp.status != 200:
                        continue
                    xml = await resp.text()
                    # Check if it's a Tune Server
                    if "Tune Server" not in xml and "TuneServer" not in xml:
                        continue
                    server = _parse_device_xml(xml, loc)
                    if server:
                        servers.append(server)
                        logger.info("tune_server_discovered",
                                    name=server.name, host=server.host, port=server.port)
            except Exception:
                continue

    return servers


def _parse_device_xml(xml: str, location: str) -> TuneServerInfo | None:
    """Extract Tune Server info from device.xml."""
    import re
    from urllib.parse import urlparse

    name_m = re.search(r"<friendlyName>(.*?)</friendlyName>", xml)
    uuid_m = re.search(r"<UDN>uuid:(.*?)</UDN>", xml)

    if not name_m or not uuid_m:
        return None

    parsed = urlparse(location)
    host = parsed.hostname or ""
    # The API port is typically 8888, the device.xml is on 8080
    # Try to detect API port from the device description or default to 8888
    api_port = 8888

    return TuneServerInfo(
        name=name_m.group(1),
        host=host,
        port=api_port,
        uuid=uuid_m.group(1),
    )


async def probe_tune_server(host: str, port: int = 8888) -> TuneServerInfo | None:
    """Probe a specific host:port to check if it's a Tune Server."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
            async with session.get(f"http://{host}:{port}/api/v1/system/health") as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if data.get("status") != "ok":
                    return None

            # Get server name from config endpoint
            name = "Tune Server"
            try:
                async with session.get(f"http://{host}:{port}/api/v1/system/version") as resp:
                    if resp.status == 200:
                        vdata = await resp.json()
                        name = vdata.get("name", name)
            except Exception:
                pass

            return TuneServerInfo(
                name=name,
                host=host,
                port=port,
                uuid="",
            )
    except Exception:
        return None

"""SSDP advertisement — announce Tune Server as a UPnP MediaServer."""
from __future__ import annotations

import asyncio
import socket
import struct

import structlog

logger = structlog.get_logger()

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
NOTIFY_INTERVAL = 60  # seconds


class SsdpAdvertiser:
    def __init__(self, device_uuid: str, server_ip: str, http_port: int) -> None:
        self._uuid = device_uuid
        self._ip = server_ip
        self._port = http_port
        self._location = f"http://{server_ip}:{http_port}/upnp/device.xml"
        self._transport = None
        self._notify_task: asyncio.Task | None = None

        self._nts = [
            ("upnp:rootdevice", f"uuid:{device_uuid}::upnp:rootdevice"),
            (f"uuid:{device_uuid}", f"uuid:{device_uuid}"),
            ("urn:schemas-upnp-org:device:MediaServer:1", f"uuid:{device_uuid}::urn:schemas-upnp-org:device:MediaServer:1"),
            ("urn:schemas-upnp-org:service:ContentDirectory:1", f"uuid:{device_uuid}::urn:schemas-upnp-org:service:ContentDirectory:1"),
            ("urn:schemas-upnp-org:service:ConnectionManager:1", f"uuid:{device_uuid}::urn:schemas-upnp-org:service:ConnectionManager:1"),
        ]

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass  # Windows
        sock.bind(("", SSDP_PORT))

        # Join multicast group on the correct interface
        mreq = struct.pack("4s4s", socket.inet_aton(SSDP_ADDR), socket.inet_aton(self._ip))
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self._ip))
        except OSError as exc:
            logger.warning("ssdp_multicast_unavailable", error=str(exc))
            sock.close()
            return
        sock.setblocking(False)

        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _SsdpProtocol(self),
            sock=sock,
        )

        self._notify_task = asyncio.create_task(self._notify_loop())
        logger.info("ssdp_advertiser_started", location=self._location)

    async def stop(self) -> None:
        if self._notify_task:
            self._notify_task.cancel()
            self._notify_task = None
        self._send_byebye()
        if self._transport:
            self._transport.close()
            self._transport = None
        logger.info("ssdp_advertiser_stopped")

    async def _notify_loop(self) -> None:
        try:
            while True:
                self._send_alive()
                await asyncio.sleep(NOTIFY_INTERVAL)
        except asyncio.CancelledError:
            pass

    def _send_alive(self) -> None:
        for nt, usn in self._nts:
            msg = (
                "NOTIFY * HTTP/1.1\r\n"
                f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
                f"CACHE-CONTROL: max-age=1800\r\n"
                f"LOCATION: {self._location}\r\n"
                f"NT: {nt}\r\n"
                "NTS: ssdp:alive\r\n"
                f"USN: {usn}\r\n"
                "SERVER: Tune/1.0 UPnP/1.0 TuneServer/0.2.2\r\n"
                "\r\n"
            )
            if self._transport:
                self._transport.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))

    def _send_byebye(self) -> None:
        for nt, usn in self._nts:
            msg = (
                "NOTIFY * HTTP/1.1\r\n"
                f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
                f"NT: {nt}\r\n"
                "NTS: ssdp:byebye\r\n"
                f"USN: {usn}\r\n"
                "\r\n"
            )
            if self._transport:
                self._transport.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))

    def respond_to_search(self, st: str, addr: tuple) -> None:
        for nt, usn in self._nts:
            if st == "ssdp:all" or st == nt:
                msg = (
                    "HTTP/1.1 200 OK\r\n"
                    f"CACHE-CONTROL: max-age=1800\r\n"
                    f"LOCATION: {self._location}\r\n"
                    f"ST: {nt}\r\n"
                    f"USN: {usn}\r\n"
                    "SERVER: Tune/1.0 UPnP/1.0 TuneServer/0.2.2\r\n"
                    "\r\n"
                )
                if self._transport:
                    self._transport.sendto(msg.encode(), addr)


class _SsdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, advertiser: SsdpAdvertiser) -> None:
        self._adv = advertiser

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            msg = data.decode("utf-8", errors="ignore")
            if not msg.startswith("M-SEARCH"):
                return
            headers = {}
            for line in msg.split("\r\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().upper()] = v.strip()
            st = headers.get("ST", "")
            if st:
                self._adv.respond_to_search(st, addr)
        except Exception:
            pass

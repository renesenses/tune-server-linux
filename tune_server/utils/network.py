from __future__ import annotations

import platform
import re
import socket
import subprocess

import structlog

logger = structlog.get_logger()

_IS_WINDOWS = platform.system() == "Windows"


def get_local_ip() -> str:
    """Get the primary local IP address.

    Uses UDP socket trick first. On Windows, falls back to ipconfig parsing
    if the socket returns a loopback or link-local address.
    """
    ip = _get_ip_via_socket()
    if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
        return ip

    # Fallback for Windows where socket trick may return wrong interface
    if _IS_WINDOWS:
        win_ip = _get_ip_via_ipconfig()
        if win_ip:
            logger.info("local_ip_from_ipconfig", ip=win_ip)
            return win_ip

    if ip:
        logger.warning("local_ip_suspect", ip=ip)
        return ip

    logger.warning("could_not_determine_local_ip")
    return "127.0.0.1"


def _get_ip_via_socket() -> str | None:
    """Determine LAN IP by opening a UDP socket to a public DNS."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return None


def _get_ip_via_ipconfig() -> str | None:
    """Parse ipconfig output on Windows to find a LAN IP (192.168.x.x or 10.x.x.x)."""
    try:
        output = subprocess.check_output(
            ["ipconfig"], timeout=5, text=True, errors="replace",
        )
        # Match IPv4 addresses that look like private LAN
        for match in re.finditer(r"IPv4[^:]*:\s*([\d.]+)", output):
            ip = match.group(1)
            if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
                return ip
    except Exception:
        logger.debug("ipconfig_parse_failed")
    return None

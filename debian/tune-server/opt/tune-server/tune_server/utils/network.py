from __future__ import annotations

import socket

import structlog

logger = structlog.get_logger()


def get_local_ip() -> str:
    """Get the primary local IP address."""
    try:
        # Connect to a public DNS to determine our LAN interface
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        logger.debug("could_not_determine_local_ip")
        return "127.0.0.1"

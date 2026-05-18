"""Admin health dashboard API routes.

Provides real-time system health, zone status, recent errors,
active connections, and discovery status for the admin dashboard.
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter

import structlog

from tune_server.api.deps import deps

logger = structlog.get_logger()

router = APIRouter(prefix="/admin", tags=["admin"])

try:
    import psutil  # type: ignore[import-untyped]
    _HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False


def _format_uptime(seconds: int) -> str:
    """Format seconds into a human-readable uptime string."""
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


@router.get("/health")
async def admin_health():
    """Real-time system health: CPU%, RAM MB, disk free, uptime, open FDs."""
    result: dict = {
        "cpu_percent": 0,
        "ram_mb": 0,
        "ram_total_mb": 0,
        "disk_free_gb": 0,
        "disk_total_gb": 0,
        "uptime_seconds": 0,
        "uptime_formatted": "0s",
        "open_fds": 0,
        "pid": os.getpid(),
        "python_threads": 0,
    }

    # Uptime from health monitor
    monitor = deps.health_monitor
    if monitor:
        uptime = monitor.uptime_seconds
        result["uptime_seconds"] = uptime
        result["uptime_formatted"] = _format_uptime(uptime)

    if _HAS_PSUTIL:
        try:
            proc = psutil.Process(os.getpid())
            # RAM
            mem = proc.memory_info()
            result["ram_mb"] = round(mem.rss / (1024 * 1024), 1)
            # Total system RAM
            vm = psutil.virtual_memory()
            result["ram_total_mb"] = round(vm.total / (1024 * 1024), 1)
            # CPU (non-blocking, uses cached value from last interval)
            result["cpu_percent"] = proc.cpu_percent(interval=0)
            # Threads
            result["python_threads"] = proc.num_threads()
        except Exception:
            logger.debug("admin_health_psutil_error", exc_info=True)

        try:
            # Disk
            import shutil
            from pathlib import Path
            from tune_server.config import settings
            db_path = getattr(settings, "db_path", "tune_server.db")
            stat = shutil.disk_usage(Path(db_path).parent)
            result["disk_free_gb"] = round(stat.free / (1024 ** 3), 1)
            result["disk_total_gb"] = round(stat.total / (1024 ** 3), 1)
        except Exception:
            logger.debug("admin_health_disk_error", exc_info=True)

    # Open file descriptors (Unix only)
    try:
        if hasattr(os, "getpid") and _HAS_PSUTIL:
            proc = psutil.Process(os.getpid())
            if hasattr(proc, "num_fds"):
                result["open_fds"] = proc.num_fds()
            else:
                # Windows: use num_handles
                result["open_fds"] = getattr(proc, "num_handles", lambda: 0)()
    except Exception:
        pass

    return result


@router.get("/zones")
async def admin_zones():
    """All zones with state, current track, output type, device, buffer status."""
    zm = deps.zone_manager
    if not zm:
        return []

    zones_data = []
    for zone in zm.list_zones():
        track = zone.current_track
        track_info = None
        if track:
            track_info = {
                "title": track.title,
                "artist_name": track.artist_name,
                "album_title": track.album_title,
                "duration_ms": track.duration_ms,
            }

        # Buffer status from player pipeline
        buffer_info = None
        try:
            player = zone._player
            pipeline = getattr(player, "_pipeline", None)
            if pipeline:
                buf = getattr(pipeline, "_buffer", None)
                if buf:
                    buffer_info = {
                        "size_kb": round(getattr(buf, "size", 0) / 1024, 1),
                        "fill_percent": round(getattr(buf, "fill_percent", 0), 1),
                    }
        except Exception:
            pass

        zones_data.append({
            "id": zone.zone_id,
            "name": zone.name,
            "state": zone.state.value if hasattr(zone.state, "value") else str(zone.state),
            "output_type": zone.output_type.value if hasattr(zone.output_type, "value") else str(zone.output_type),
            "device_name": zone.output_device_id or "default",
            "online": getattr(zone, "_online", True),
            "current_track": track_info,
            "position_ms": zone.position_ms,
            "volume": getattr(zone._player, "volume", 0),
            "buffer": buffer_info,
            "group_id": getattr(zone, "_group_id", None),
        })

    return zones_data


@router.get("/errors")
async def admin_errors():
    """Last 50 errors with timestamp, level, event, details."""
    try:
        from tune_server.utils.error_buffer import recent_errors
        errors = recent_errors(50)
        # Return newest-first for the dashboard
        return list(reversed(errors))
    except Exception:
        logger.debug("admin_errors_fetch_failed", exc_info=True)
        return []


@router.get("/connections")
async def admin_connections():
    """Active WebSocket connections count and HTTP streamer sessions."""
    result = {
        "websocket_connections": 0,
        "http_streamer_sessions": 0,
    }

    # WebSocket connections via the module-level _ws_manager
    try:
        from tune_server.api.main import _ws_manager
        if _ws_manager:
            result["websocket_connections"] = len(_ws_manager._subscriptions)
    except Exception:
        pass

    # HTTP streamer sessions: discover via zone outputs
    # The http_streamer is on TuneServer (app.py), not in deps.
    # We can count active sessions by scanning zone output targets.
    try:
        zm = deps.zone_manager
        if zm:
            active_streams = 0
            for zone in zm.list_zones():
                try:
                    output = zone._output
                    if output and hasattr(output, "_streamer"):
                        streamer = output._streamer
                        if hasattr(streamer, "_sessions"):
                            active_streams = len(streamer._sessions)
                            break  # All zones share the same streamer
                except Exception:
                    continue
            result["http_streamer_sessions"] = active_streams
    except Exception:
        pass

    return result


@router.get("/discovery")
async def admin_discovery():
    """SSDP/mDNS/AirPlay/Chromecast discovered devices with protocol info."""
    dm = deps.discovery_manager
    if not dm:
        return {"devices": [], "protocols": {}}

    devices_data = []
    for device in dm.list_devices():
        devices_data.append({
            "id": device.id,
            "name": device.name,
            "type": device.type.value if hasattr(device.type, "value") else str(device.type),
            "host": device.host,
            "port": device.port,
            "available": device.available,
            "capabilities": device.capabilities,
        })

    # Protocol status
    protocols = {
        "ssdp": dm._ssdp is not None,
        "mdns": dm._mdns is not None,
        "chromecast": getattr(dm, "_cast", None) is not None,
        "bluos": getattr(dm, "_bluos", None) is not None,
        "openhome": getattr(dm, "_openhome", None) is not None,
        "squeezebox": getattr(dm, "_squeezebox", None) is not None,
    }

    return {
        "devices": devices_data,
        "protocols": protocols,
        "device_count": len(devices_data),
    }

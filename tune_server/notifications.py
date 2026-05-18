"""Desktop notifications for track changes.

Subscribes to EventBus PLAYBACK_TRACK_CHANGED events and shows a native
OS notification with track title, artist, and album artwork.

Platforms:
- macOS: osascript (Notification Center)
- Linux: notify-send (libnotify)
- Windows: PowerShell toast notification (no extra dependency)

Controlled by TUNE_NOTIFICATIONS_ENABLED env var (default: false).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from tune_server.event_bus import Event, EventBus

logger = structlog.get_logger()

_PLATFORM = sys.platform  # "darwin", "linux", "win32"


def _is_enabled() -> bool:
    """Check TUNE_NOTIFICATIONS_ENABLED env var (default false)."""
    val = os.environ.get("TUNE_NOTIFICATIONS_ENABLED", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _notification_icon_dir() -> Path:
    """Return a temp directory for cached notification icons."""
    d = Path(tempfile.gettempdir()) / "tune-notify-icons"
    d.mkdir(exist_ok=True)
    return d


async def _download_icon(cover_url: str, server_base: str) -> str | None:
    """Download cover art to a local temp file for use as notification icon.

    cover_url may be:
    - An absolute HTTP(S) URL (streaming CDN)
    - A server-relative path like /api/v1/library/artwork/xxx.jpg
    - A local filesystem path (artwork cache)

    Returns the local file path, or None on failure.
    """
    if not cover_url:
        return None

    # Local file already on disk
    if not cover_url.startswith("http") and not cover_url.startswith("/api/"):
        if Path(cover_url).is_file():
            return cover_url
        return None

    # Build full URL for server-relative paths
    url = cover_url
    if cover_url.startswith("/api/"):
        url = f"{server_base}{cover_url}"

    # Cache key based on URL (strip query params for stability)
    base_url = url.split("?")[0]
    cache_key = hashlib.md5(base_url.encode()).hexdigest()
    icon_path = _notification_icon_dir() / f"{cache_key}.jpg"

    if icon_path.exists() and icon_path.stat().st_size > 0:
        return str(icon_path)

    try:

        def _do_download() -> bool:
            try:
                result = subprocess.run(
                    ["curl", "-4sfL", "--max-time", "5",
                     "-o", str(icon_path), url],
                    capture_output=True, timeout=8,
                )
                return (
                    result.returncode == 0
                    and icon_path.exists()
                    and icon_path.stat().st_size > 0
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return False

        ok = await asyncio.to_thread(_do_download)
        if ok:
            return str(icon_path)
    except Exception:
        logger.debug("notification_icon_download_failed", url=url, exc_info=True)

    return None


async def _show_macos(title: str, subtitle: str, icon_path: str | None) -> None:
    """Show a macOS Notification Center notification via osascript."""
    # AppleScript display notification
    script = f'display notification "{_escape_applescript(subtitle)}" with title "{_escape_applescript(title)}"'
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", script],
            capture_output=True, timeout=5,
        )
    except Exception:
        logger.debug("macos_notification_failed", exc_info=True)


def _escape_applescript(s: str) -> str:
    """Escape a string for embedding in an AppleScript double-quoted string."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


async def _show_linux(title: str, body: str, icon_path: str | None) -> None:
    """Show a Linux desktop notification via notify-send (libnotify)."""
    cmd = ["notify-send", "--app-name=Tune", "-t", "5000"]
    if icon_path:
        cmd.extend(["-i", icon_path])
    cmd.extend([title, body])
    try:
        await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True, timeout=5,
        )
    except Exception:
        logger.debug("linux_notification_failed", exc_info=True)


async def _show_windows(title: str, body: str, icon_path: str | None) -> None:
    """Show a Windows toast notification via PowerShell."""
    # Use the built-in Windows toast via PowerShell (no external dependency)
    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] | Out-Null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, "
        "ContentType = WindowsRuntime] | Out-Null; "
        "$template = '<toast><visual><binding template=\"ToastGeneric\">"
        f"<text>{_escape_xml(title)}</text>"
        f"<text>{_escape_xml(body)}</text>"
    )
    if icon_path:
        ps_script += f'<image placement="appLogoOverride" src="{_escape_xml(icon_path)}"/>'
    ps_script += (
        "</binding></visual></toast>'; "
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        "$xml.LoadXml($template); "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Tune Server')"
        ".Show($toast)"
    )
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        logger.debug("windows_notification_failed", exc_info=True)


def _escape_xml(s: str) -> str:
    """Escape special XML characters."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


async def _show_notification(title: str, body: str, icon_path: str | None) -> None:
    """Dispatch to the platform-specific notification method."""
    if _PLATFORM == "darwin":
        await _show_macos(title, body, icon_path)
    elif _PLATFORM.startswith("linux"):
        await _show_linux(title, body, icon_path)
    elif _PLATFORM == "win32":
        await _show_windows(title, body, icon_path)
    else:
        logger.debug("notifications_unsupported_platform", platform=_PLATFORM)


def setup_notifications(event_bus: EventBus, server_ip: str, api_port: int) -> None:
    """Subscribe to track change events and show desktop notifications.

    Called from TuneServer.boot() in app.py. Does nothing if
    TUNE_NOTIFICATIONS_ENABLED is not set.
    """
    if not _is_enabled():
        logger.debug("desktop_notifications_disabled")
        return

    from tune_server.event_bus import EventType

    server_base = f"http://{server_ip}:{api_port}"

    async def _on_track_changed(event: Event) -> None:
        data = event.data or {}
        track_title = data.get("track_title")
        if not track_title:
            return

        artist = data.get("artist_name") or ""
        album = data.get("album_title") or ""
        cover = data.get("cover_path")

        body_parts = [p for p in (artist, album) if p]
        body = " — ".join(body_parts) if body_parts else ""

        # Download icon in background (non-blocking, best-effort)
        icon_path: str | None = None
        try:
            icon_path = await _download_icon(cover, server_base)
        except Exception:
            pass

        await _show_notification(track_title, body, icon_path)

    event_bus.on(EventType.PLAYBACK_TRACK_CHANGED, _on_track_changed)
    logger.info("desktop_notifications_enabled", platform=_PLATFORM)

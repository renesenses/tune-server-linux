"""Auto-update checker and installer for Tune Server.

Periodically checks GitHub Releases for new versions.
Notifies the user via WebSocket. Installs on confirmation, OR
auto-installs and restarts when settings.auto_update is True.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import signal
import sys
import tempfile
import zipfile
import tarfile
from pathlib import Path

import aiohttp
import structlog

from tune_server import __version__
from tune_server.config import settings

logger = structlog.get_logger()

GITHUB_REPO = "renesenses/tune-server-linux"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CHECK_INTERVAL_HOURS = 6


def _get_platform_asset_name(version: str) -> str:
    """Return the expected asset filename for this platform."""
    system = platform.system().lower()
    if system == "windows":
        return f"tune-server-{version}-windows.zip"
    elif system == "darwin":
        # Check if ARM or Intel
        machine = platform.machine().lower()
        if "arm" in machine or "aarch64" in machine:
            return f"tune-server-{version}-macos.tar.gz"
        return f"tune-server-{version}-macos-intel.tar.gz"
    return f"tune-server-{version}-linux.tar.gz"


class UpdateChecker:
    """Checks for updates and manages the update process."""

    def __init__(self, event_bus=None):
        self._event_bus = event_bus
        self._latest_version: str | None = None
        self._download_url: str | None = None
        self._asset_name: str | None = None
        self._check_task: asyncio.Task | None = None

    @property
    def current_version(self) -> str:
        return __version__

    @property
    def update_available(self) -> bool:
        return self._latest_version is not None and self._latest_version != self.current_version

    @property
    def latest_version(self) -> str | None:
        return self._latest_version

    def start(self) -> None:
        """Start periodic update checking."""
        self._check_task = asyncio.ensure_future(self._check_loop())

    def stop(self) -> None:
        if self._check_task:
            self._check_task.cancel()

    async def _check_loop(self) -> None:
        """Check for updates periodically."""
        # Initial check after 30 seconds
        await asyncio.sleep(30)
        while True:
            try:
                info = await self.check_for_update()
                if info and settings.auto_update:
                    await self._auto_install_and_restart()
            except Exception:
                logger.debug("update_check_failed")
            await asyncio.sleep(CHECK_INTERVAL_HOURS * 3600)

    async def _auto_install_and_restart(self) -> None:
        """Download + install + restart, no UI click needed.

        Skipped on Windows: the running tune-server.exe is locked by the
        OS and shutil.copy2 will fail. Stage-and-swap on next start is
        deferred to v0.7.20+. For now, Windows users keep the manual
        "Installer" button which prompts them to restart manually.
        """
        if platform.system().lower() == "windows":
            logger.info(
                "auto_update_skipped_windows",
                hint="manual install via UI required; running .exe is file-locked",
            )
            return

        logger.info("auto_update_starting", version=self._latest_version)
        ok = await self.download_and_install()
        if not ok:
            logger.warning("auto_update_install_failed")
            return

        # systemd / launchd / supervisord will restart us; for plain
        # `python -m tune_server` runs the user has to relaunch manually.
        logger.info("auto_update_restarting")
        # Give the event bus a chance to flush the update_installed event
        # before SIGTERM kills us.
        await asyncio.sleep(2)
        os.kill(os.getpid(), signal.SIGTERM)

    async def check_for_update(self) -> dict | None:
        """Check GitHub for the latest release. Returns update info or None."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(GITHUB_API, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()

            tag = data.get("tag_name", "").lstrip("v")
            if not tag or tag == self.current_version:
                self._latest_version = None
                return None

            # Compare versions
            if not self._is_newer(tag, self.current_version):
                return None

            # Find the right asset for this platform
            asset_name = _get_platform_asset_name(tag)
            download_url = None
            asset_size = 0
            for asset in data.get("assets", []):
                if asset["name"] == asset_name:
                    download_url = asset["browser_download_url"]
                    asset_size = asset.get("size", 0)
                    break

            if not download_url:
                logger.warning("update_no_asset", version=tag, platform=asset_name)
                return None

            self._latest_version = tag
            self._download_url = download_url
            self._asset_name = asset_name

            info = {
                "current_version": self.current_version,
                "latest_version": tag,
                "download_url": download_url,
                "asset_name": asset_name,
                "asset_size": asset_size,
                "release_notes": data.get("body", ""),
            }

            logger.info("update_available", current=self.current_version, latest=tag)

            # Notify via event bus
            if self._event_bus:
                from tune_server.event_bus import Event, EventType
                await self._event_bus.emit(Event(
                    type=EventType.SYSTEM_UPDATE_AVAILABLE,
                    data=info,
                ))

            return info

        except Exception:
            logger.debug("update_check_error")
            return None

    async def download_and_install(self) -> bool:
        """Download the update and install it. Returns True on success."""
        if not self._download_url or not self._asset_name:
            return False

        logger.info("update_downloading", version=self._latest_version, asset=self._asset_name)

        try:
            # Download to temp directory
            tmp_dir = Path(tempfile.mkdtemp(prefix="tune-update-"))
            archive_path = tmp_dir / self._asset_name

            async with aiohttp.ClientSession() as session:
                async with session.get(self._download_url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                    if resp.status != 200:
                        logger.error("update_download_failed", status=resp.status)
                        return False
                    with open(archive_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            f.write(chunk)

            logger.info("update_downloaded", path=str(archive_path))

            # Extract
            extract_dir = tmp_dir / "extracted"
            if archive_path.suffix == ".zip":
                with zipfile.ZipFile(archive_path) as zf:
                    zf.extractall(extract_dir)
            elif archive_path.name.endswith(".tar.gz"):
                with tarfile.open(archive_path, "r:gz") as tf:
                    tf.extractall(extract_dir)

            # Find the tune-server directory in the extracted archive
            extracted_dirs = list(extract_dir.iterdir())
            source_dir = extracted_dirs[0] if len(extracted_dirs) == 1 and extracted_dirs[0].is_dir() else extract_dir

            # Backup current installation
            if getattr(sys, "frozen", False):
                exe_dir = Path(sys.executable).resolve().parent
            else:
                exe_dir = Path.cwd()

            backup_dir = exe_dir / f"_backup_{self.current_version}"
            if backup_dir.exists():
                shutil.rmtree(backup_dir)

            # Copy new files (skip data files)
            data_files = {".env", "tune_server.db", "artwork_cache", "backups"}
            for item in source_dir.iterdir():
                if item.name in data_files:
                    continue
                dst = exe_dir / item.name
                if item.is_dir():
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(str(item), str(dst))
                else:
                    shutil.copy2(str(item), str(dst))

            # Cleanup temp
            shutil.rmtree(tmp_dir, ignore_errors=True)

            logger.info("update_installed", version=self._latest_version)

            # Notify
            if self._event_bus:
                from tune_server.event_bus import Event, EventType
                await self._event_bus.emit(Event(
                    type=EventType.SYSTEM_UPDATE_INSTALLED,
                    data={"version": self._latest_version, "restart_required": True},
                ))

            return True

        except Exception:
            logger.exception("update_install_error")
            return False

    @staticmethod
    def _is_newer(new_version: str, current_version: str) -> bool:
        """Compare semver strings."""
        try:
            new_parts = [int(x) for x in new_version.split(".")]
            cur_parts = [int(x) for x in current_version.split(".")]
            return new_parts > cur_parts
        except (ValueError, AttributeError):
            return False

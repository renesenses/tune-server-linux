"""Remote plugin catalog + pip-based install/uninstall manager.

Fetches the plugin catalog from mozaiklabs.fr, cross-references with
locally installed plugins, and manages installation via pip subprocess.

On frozen builds (PyInstaller), pip is unavailable — plugins are installed
by downloading wheels from PyPI and extracting them into a local plugins/
directory that is added to sys.path at startup.
"""
from __future__ import annotations

import asyncio
import platform
import shutil
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import structlog

from tune_server import __version__
from tune_server.plugins.loader import PluginLoader

logger = structlog.get_logger()

CATALOG_URL = "https://mozaiklabs.fr/api/v1/plugins"
TRACK_URL = "https://mozaiklabs.fr/api/v1/plugins/{name}/track-install"
CACHE_TTL = 300

_IS_FROZEN = getattr(sys, "frozen", False)


def _plugins_dir() -> Path:
    """User-plugins directory next to the frozen executable."""
    if _IS_FROZEN:
        return Path(sys.executable).resolve().parent / "plugins"
    return Path.cwd() / "plugins"


def ensure_plugins_on_path() -> None:
    """Add the user-plugins directory to sys.path (called once at startup)."""
    d = _plugins_dir()
    if d.is_dir():
        s = str(d)
        if s not in sys.path:
            sys.path.insert(0, s)
            logger.info("plugins_dir_on_path", path=s)


class PluginStoreManager:
    """Manages the remote plugin catalog and pip-based install/uninstall."""

    def __init__(self) -> None:
        self._catalog_cache: list[dict[str, Any]] | None = None
        self._cache_time: float = 0
        self._install_lock = asyncio.Lock()

    async def fetch_catalog(self, force: bool = False) -> list[dict[str, Any]]:
        """Fetch the plugin catalog from mozaiklabs.fr. Cached for 5 min."""
        import time

        if not force and self._catalog_cache is not None:
            if time.monotonic() - self._cache_time < CACHE_TTL:
                return self._catalog_cache

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(CATALOG_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        self._catalog_cache = await resp.json()
                        self._cache_time = time.monotonic()
                        logger.info("plugin_catalog_fetched", count=len(self._catalog_cache))
                        return self._catalog_cache
                    logger.warning("plugin_catalog_fetch_failed", status=resp.status)
        except Exception:
            logger.debug("plugin_catalog_fetch_error", exc_info=True)

        return self._catalog_cache or []

    async def list_merged(self, plugin_loader: PluginLoader) -> list[dict[str, Any]]:
        """Return merged list of remote catalog + locally installed plugins."""
        catalog = await self.fetch_catalog()
        local_plugins = {p["name"]: p for p in plugin_loader.list_plugins()}
        result: list[dict[str, Any]] = []

        for remote in catalog:
            name = remote.get("name", "")
            local = local_plugins.pop(name, None)
            compatible = self._is_compatible(remote)

            entry = {
                **remote,
                "compatible": compatible,
                "installed": local is not None,
                "installed_version": local["version"] if local else None,
                "update_available": (
                    local is not None
                    and local.get("version", "?") != "?"
                    and remote.get("version", "") != local.get("version", "")
                ),
                "status": local["status"] if local else "available",
            }
            result.append(entry)

        for name, local in local_plugins.items():
            result.append({
                "name": name,
                "display_name": name,
                "description": local.get("description", ""),
                "version": local.get("version", "?"),
                "category": "tools",
                "compatible": True,
                "installed": True,
                "installed_version": local.get("version", "?"),
                "update_available": False,
                "status": local.get("status", "active"),
                "source": "local",
            })

        result.sort(key=lambda p: (not p.get("installed"), not p.get("is_featured", False), p.get("display_name", "")))
        return result

    def _is_compatible(self, plugin: dict[str, Any]) -> bool:
        """Check if a plugin is compatible with the running Tune version."""
        try:
            from packaging.version import Version

            current = Version(__version__)
            min_v = plugin.get("min_tune_version")
            max_v = plugin.get("max_tune_version")

            if min_v and current < Version(min_v):
                return False
            if max_v and current > Version(max_v):
                return False
        except Exception:
            pass
        return True

    async def install(self, name: str) -> dict[str, Any]:
        """Install a plugin via pip (or wheel extraction on frozen builds)."""
        catalog = await self.fetch_catalog()
        plugin = next((p for p in catalog if p.get("name") == name), None)
        if not plugin:
            return {"success": False, "error": f"Plugin '{name}' not found in catalog"}

        install_source = plugin.get("install_source", name)

        async with self._install_lock:
            if _IS_FROZEN:
                return await self._wheel_install(install_source, name)
            return await self._pip_action("install", install_source, name)

    async def uninstall(self, name: str) -> dict[str, Any]:
        """Uninstall a plugin via pip (or remove from plugins/ on frozen builds)."""
        async with self._install_lock:
            if _IS_FROZEN:
                return await self._wheel_uninstall(name)

            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "uninstall", "-y", name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            success = proc.returncode == 0
            if success:
                asyncio.create_task(self._track_event(name, "uninstall"))

            return {
                "success": success,
                "message": stdout.decode() if success else stderr.decode(),
                "restart_required": True,
            }

    async def update(self, name: str) -> dict[str, Any]:
        """Update a plugin via pip (or re-download wheel on frozen builds)."""
        catalog = await self.fetch_catalog()
        plugin = next((p for p in catalog if p.get("name") == name), None)
        install_source = plugin.get("install_source", name) if plugin else name

        async with self._install_lock:
            if _IS_FROZEN:
                await self._wheel_uninstall(name)
                return await self._wheel_install(install_source, name, event="update")
            return await self._pip_action("install", f"{install_source} --upgrade", name, event="update")

    # ------------------------------------------------------------------
    # Frozen build: wheel-based install (no pip required)
    # ------------------------------------------------------------------

    async def _wheel_install(self, install_source: str, plugin_name: str, event: str = "install") -> dict[str, Any]:
        """Download a wheel from PyPI and extract it into plugins/."""
        import aiohttp

        pkg_name = install_source.split("[")[0].split("=")[0].split(">")[0].split("<")[0].strip()
        pypi_url = f"https://pypi.org/pypi/{pkg_name}/json"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(pypi_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return {"success": False, "error": f"Package '{pkg_name}' not found on PyPI (HTTP {resp.status})"}
                    data = await resp.json()

            urls = data.get("urls", [])
            whl = next((u for u in urls if u["filename"].endswith(".whl")
                        and ("py3" in u["filename"] or "none" in u["filename"])), None)
            if not whl:
                whl = next((u for u in urls if u["filename"].endswith(".whl")), None)
            if not whl:
                return {"success": False, "error": f"No wheel found for '{pkg_name}' on PyPI"}

            async with aiohttp.ClientSession() as session:
                async with session.get(whl["url"], timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        return {"success": False, "error": f"Failed to download wheel (HTTP {resp.status})"}
                    wheel_bytes = await resp.read()

            dest = _plugins_dir()
            dest.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(BytesIO(wheel_bytes)) as zf:
                zf.extractall(dest)

            ensure_plugins_on_path()

            asyncio.create_task(self._track_event(plugin_name, event))
            logger.info("plugin_wheel_installed", plugin=plugin_name, dest=str(dest))

            return {
                "success": True,
                "message": f"Installed {pkg_name} to {dest}",
                "restart_required": True,
            }
        except Exception as exc:
            logger.exception("plugin_wheel_install_error", plugin=plugin_name)
            return {"success": False, "error": str(exc)}

    async def _wheel_uninstall(self, name: str) -> dict[str, Any]:
        """Remove a wheel-installed plugin from plugins/."""
        dest = _plugins_dir()
        if not dest.is_dir():
            return {"success": False, "error": "No plugins directory"}

        pkg_prefix = name.replace("-", "_")
        removed = False
        for item in dest.iterdir():
            if item.name.startswith(pkg_prefix):
                if item.is_dir():
                    shutil.rmtree(item)
                    removed = True
                elif item.is_file():
                    item.unlink()
                    removed = True

        if removed:
            asyncio.create_task(self._track_event(name, "uninstall"))
            logger.info("plugin_wheel_uninstalled", plugin=name)
            return {"success": True, "message": f"Removed {name}", "restart_required": True}

        return {"success": False, "error": f"Plugin '{name}' not found in {dest}"}

    # ------------------------------------------------------------------
    # pip-based install (source/venv installs)
    # ------------------------------------------------------------------

    async def _pip_action(
        self, action: str, target: str, plugin_name: str, event: str = "install"
    ) -> dict[str, Any]:
        """Run a pip action and track the result."""
        args = [sys.executable, "-m", "pip", action]
        args.extend(target.split())

        logger.info("plugin_pip_action", action=action, target=target)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        success = proc.returncode == 0

        if not success and b"Permission denied" in stderr:
            logger.info("plugin_pip_retry_user", plugin=plugin_name)
            user_args = [sys.executable, "-m", "pip", action, "--user"]
            user_args.extend(target.split())
            proc = await asyncio.create_subprocess_exec(
                *user_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            success = proc.returncode == 0

        if success:
            asyncio.create_task(self._track_event(plugin_name, event))
            logger.info("plugin_pip_success", plugin=plugin_name, action=action)
        else:
            logger.warning("plugin_pip_failed", plugin=plugin_name, stderr=stderr.decode()[:500])

        return {
            "success": success,
            "message": stdout.decode() if success else stderr.decode(),
            "restart_required": success,
        }

    async def _track_event(self, plugin_name: str, event: str) -> None:
        """Fire-and-forget install tracking to mozaiklabs.fr."""
        try:
            import aiohttp

            url = TRACK_URL.format(name=plugin_name)
            payload = {
                "tune_version": __version__,
                "platform": platform.system().lower(),
                "event": event,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)):
                    pass
        except Exception:
            pass


async def auto_install_plugins(plugins_csv: str) -> None:
    """Install plugins listed in TUNE_INSTALL_PLUGINS env var.

    Called once at startup before plugin discovery. Packages already
    installed are skipped by pip (fast no-op).
    """
    if _IS_FROZEN:
        logger.info("auto_install_skipped_frozen")
        return

    packages = [p.strip() for p in plugins_csv.split(",") if p.strip()]
    if not packages:
        return

    logger.info("auto_install_plugins", packages=packages)

    for pkg in packages:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--quiet", pkg,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.info("auto_install_ok", package=pkg)
        else:
            logger.warning("auto_install_failed", package=pkg, error=stderr.decode()[:200])

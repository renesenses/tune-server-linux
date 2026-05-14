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


# Windows-specific update applier. Spawned detached from tune-server.exe;
# waits for the .exe to release file locks, robocopies the staged bundle
# over the live install (preserving user data), restarts the launcher,
# then deletes itself. Must be ASCII-safe (cmd.exe is unforgiving on UTF-8
# in batch files).
_WINDOWS_APPLY_UPDATE_BAT = r"""@echo off
REM ===========================================================================
REM Tune Server -- staged-update applier (Windows)
REM ===========================================================================
REM Spawned detached by tune-server.exe right before the parent process exits
REM (SIGTERM). Job: confirm the parent is gone, mirror _update_staging\ over
REM the install dir, then relaunch.
REM
REM Logs everything to _update.log so a tester can share it when the update
REM ends in a bad state ("UI still says old version after restart" was the
REM symptom Yves hit on 2026-04-30 going from 0.6.5 -> 0.7.56 -- file handles
REM weren't released in time, robocopy retried, but start-tune-server.bat
REM had already relaunched the OLD exe in parallel).
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "LOGFILE=%~dp0_update.log"
echo [%DATE% %TIME%] tune-update applier starting > "%LOGFILE%"
echo [%DATE% %TIME%] cwd: %cd% >> "%LOGFILE%"

REM Sanity: staging must exist + contain the new tune-server.exe.
if not exist "_update_staging" (
    echo [%DATE% %TIME%] [ERROR] _update_staging folder missing -- aborting >> "%LOGFILE%"
    echo [tune-update][ERROR] _update_staging folder missing. See _update.log
    exit /b 1
)
if not exist "_update_staging\tune-server.exe" (
    echo [%DATE% %TIME%] [ERROR] _update_staging\tune-server.exe missing -- aborting >> "%LOGFILE%"
    echo [tune-update][ERROR] Staged update incomplete. See _update.log
    exit /b 1
)

REM 1. Wait for the parent process to actually release its file handles.
REM    Polling tasklist is more reliable than a fixed sleep: Yves hit a case
REM    where 4 s wasn't enough on a slow Windows install.
echo [%DATE% %TIME%] Polling for tune-server.exe exit (max 30 s)... >> "%LOGFILE%"
set /a TRIES=0
:wait_exit
tasklist /FI "IMAGENAME eq tune-server.exe" /FO CSV /NH 2>nul | findstr /I "tune-server.exe" >nul
if errorlevel 1 goto :proc_gone
set /a TRIES+=1
if %TRIES% GEQ 15 (
    echo [%DATE% %TIME%] tune-server.exe still alive after 30 s -- force-killing >> "%LOGFILE%"
    taskkill /IM tune-server.exe /F >> "%LOGFILE%" 2>&1
    ping -n 4 127.0.0.1 >nul
    goto :proc_gone
)
ping -n 3 127.0.0.1 >nul
goto :wait_exit
:proc_gone

REM 2. Backstop: kill librespot and any orphan helper too. Multiple cmd
REM    windows from start-tune-server.bat can keep the dir locked.
taskkill /IM librespot.exe /F >> "%LOGFILE%" 2>&1

REM 3. Extra grace period -- Windows takes ~1-2 s after process exit to
REM    actually release file locks on the .exe.
echo [%DATE% %TIME%] Process gone, waiting 3 s for file handle release... >> "%LOGFILE%"
ping -n 4 127.0.0.1 >nul

REM 4. Mirror the staged bundle on top of the install dir. /MIR removes
REM    files no longer in the new build (clean upgrade) but excludes user
REM    state. /R:10 /W:3 = 10 retries x 3 s wait each (30 s total grace
REM    for any handle the OS hasn't released yet).
echo [%DATE% %TIME%] robocopy _update_staging\ -> . starting... >> "%LOGFILE%"
robocopy "_update_staging" "." /MIR ^
    /XF .env tune_server.db tune-server.log tune-server.log.1 _update.log ^
    /XD artwork_cache backups _update_staging _backup_* ^
    /R:10 /W:3 /NP /NJH /NJS >> "%LOGFILE%" 2>&1

set RC=%errorlevel%
echo [%DATE% %TIME%] robocopy returned %RC% >> "%LOGFILE%"

REM robocopy success codes are 0-7; 8+ is failure.
if %RC% GEQ 8 (
    echo [%DATE% %TIME%] [ERROR] robocopy failed (code %RC%) -- install dir may be partial >> "%LOGFILE%"
    echo [tune-update][ERROR] robocopy failed with code %RC%. See _update.log
    exit /b 1
)

REM 5. Sanity-check: confirm the live tune-server.exe really got swapped.
REM    Compare file sizes between staging and install -- they should match.
for %%I in ("_update_staging\tune-server.exe") do set "STAGING_SIZE=%%~zI"
for %%I in ("tune-server.exe") do set "LIVE_SIZE=%%~zI"
echo [%DATE% %TIME%] sizes: staging=%STAGING_SIZE% live=%LIVE_SIZE% >> "%LOGFILE%"
if not "%STAGING_SIZE%"=="%LIVE_SIZE%" (
    echo [%DATE% %TIME%] [WARN] sizes differ -- robocopy did NOT replace tune-server.exe (locked?) >> "%LOGFILE%"
    echo [tune-update][WARN] tune-server.exe size mismatch -- see _update.log
    REM Try one more time directly with copy /Y as a fallback for the exe
    REM (robocopy can mis-handle a file held briefly by its own retry loop).
    copy /Y "_update_staging\tune-server.exe" "tune-server.exe" >> "%LOGFILE%" 2>&1
)

REM 6. Cleanup staging.
rmdir /S /Q "_update_staging" 2>nul

echo [%DATE% %TIME%] Update applied successfully. Relaunching... >> "%LOGFILE%"
if exist "start-tune-server.bat" (
    start "" "%~dp0start-tune-server.bat"
) else (
    start "" "%~dp0tune-server.exe"
)

REM Note: this bat is NOT self-deleted. cmd holds the file handle while
REM running and the workarounds (delayed del/rmdir) are flaky across
REM Windows versions. The next update simply overwrites this file.

endlocal
exit /b 0
"""


def _get_platform_asset_name(version: str) -> str:
    """Return the expected asset filename for this platform."""
    system = platform.system().lower()
    if system == "windows":
        return f"tune-server-{version}-windows-setup.exe"
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

    @property
    def is_source_install(self) -> bool:
        """True when running from a git clone + venv (e.g. .18 dev/prod box).

        The release tarballs ship a PyInstaller bundle (`_internal/`,
        `tune-server` binary, `librespot`, `install.sh`, ...) and
        unpacking that on top of a source checkout pollutes the repo and
        leaves the venv's installed `tune-server` package out of sync.
        Detect the case so we can refuse the in-app installer with a
        clear message instead of silently breaking the install.
        """
        if getattr(sys, "frozen", False):
            return False
        cwd = Path.cwd()
        return (cwd / "pyproject.toml").is_file() and (cwd / ".git").exists()

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
        """Download + install + restart, no UI click needed."""
        logger.info("auto_update_starting", version=self._latest_version)
        ok = await self.download_and_install()
        if not ok:
            logger.warning("auto_update_install_failed")
            return

        # On Windows the swap happens in a detached helper .bat that
        # waits for our process to exit. We just need to trigger our own
        # shutdown and let the helper take over.
        if platform.system().lower() == "windows":
            self._spawn_windows_apply_helper()
            logger.info("auto_update_handing_off_to_windows_helper")
            await asyncio.sleep(2)
            os.kill(os.getpid(), signal.SIGTERM)
            return

        # systemd / launchd / supervisord will restart us; for plain
        # `python -m tune_server` runs the user has to relaunch manually.
        logger.info("auto_update_restarting")
        # Give the event bus a chance to flush the update_installed event
        # before SIGTERM kills us.
        await asyncio.sleep(2)
        os.kill(os.getpid(), signal.SIGTERM)

    def _spawn_windows_apply_helper(self) -> None:
        """Launch a detached helper .bat that orchestrates the NSIS update.

        The helper:
        1. Creates an ``_update_pending`` sentinel so the watchdog in
           ``start-tune-server.bat`` knows NOT to restart tune-server.exe.
        2. Kills the CMD watchdog window (title "Tune Server") to prevent
           it from racing the installer.
        3. Waits for tune-server.exe to fully exit (polls tasklist, max 30 s,
           force-kills if stuck).
        4. Kills librespot.exe (helper child that holds file locks).
        5. Runs the NSIS setup.exe silently (``/S /D=install_dir``).
        6. Removes the sentinel.
        7. Relaunches start-tune-server.bat (or tune-server.exe directly).

        Robust against slow exits, file locks, and French-locale cmd.exe.
        Everything is logged to ``_update.log`` for tester diagnostics.
        """
        import subprocess
        if not getattr(sys, "frozen", False):
            return

        installer_path = getattr(self, "_windows_installer_path", None)
        if not installer_path or not Path(installer_path).is_file():
            logger.warning("update_installer_missing", path=str(installer_path))
            return

        exe_dir = Path(sys.executable).resolve().parent
        install_dir = str(exe_dir)

        # Create sentinel so the watchdog steps aside if it somehow sees
        # the exit before we kill its window. The bat also re-creates it
        # as a belt-and-suspenders measure.
        sentinel = exe_dir / "_update_pending"
        sentinel.write_text("update in progress", encoding="ascii")
        logger.info("update_sentinel_created", path=str(sentinel))

        helper_script = f"""@echo off
setlocal enabledelayedexpansion
cd /d "{install_dir}"

set "LOGFILE={install_dir}\\_update.log"
echo [%DATE% %TIME%] NSIS silent update helper starting > "%LOGFILE%"
echo [%DATE% %TIME%] installer: {installer_path} >> "%LOGFILE%"
echo [%DATE% %TIME%] install_dir: {install_dir} >> "%LOGFILE%"

REM Create sentinel so the watchdog steps aside on any exit code.
echo update > "_update_pending"

REM Kill the CMD watchdog window by title. start-tune-server.bat sets
REM 'title Tune Server' on line 2, so taskkill /FI matches it.
REM Without this, the watchdog may relaunch tune-server.exe between
REM our SIGTERM and the NSIS installer starting.
echo [%DATE% %TIME%] Killing watchdog CMD window... >> "%LOGFILE%"
taskkill /FI "WINDOWTITLE eq Tune Server" /F >> "%LOGFILE%" 2>&1
REM Also try the localized title pattern (some Windows builds append a dash)
taskkill /FI "WINDOWTITLE eq Tune Server*" /F >> "%LOGFILE%" 2>&1

REM Wait for tune-server.exe to actually release its file handles.
echo [%DATE% %TIME%] Polling for tune-server.exe exit (max 30 s)... >> "%LOGFILE%"
set /a TRIES=0
:wait_exit
tasklist /FI "IMAGENAME eq tune-server.exe" /FO CSV /NH 2>nul | findstr /I "tune-server.exe" >nul
if errorlevel 1 goto :proc_gone
set /a TRIES+=1
if !TRIES! GEQ 15 (
    echo [%DATE% %TIME%] tune-server.exe still alive after 30 s -- force-killing >> "%LOGFILE%"
    taskkill /IM tune-server.exe /F >> "%LOGFILE%" 2>&1
    ping -n 4 127.0.0.1 >nul
    goto :proc_gone
)
ping -n 3 127.0.0.1 >nul
goto :wait_exit
:proc_gone

REM Kill librespot and any orphan helpers that may hold file locks.
taskkill /IM librespot.exe /F >> "%LOGFILE%" 2>&1

REM Grace period for Windows to release file handles after process exit.
echo [%DATE% %TIME%] Process gone, waiting 3 s for handle release... >> "%LOGFILE%"
ping -n 4 127.0.0.1 >nul

REM Run the NSIS installer silently. /S = silent, /D= sets install dir.
REM The NSIS installer's own pre-install hook kills tune-server.exe
REM (redundant but harmless since we already did it).
echo [%DATE% %TIME%] Running NSIS installer silently... >> "%LOGFILE%"
"{installer_path}" /S /D={install_dir}
set NSIS_RC=!errorlevel!
echo [%DATE% %TIME%] NSIS installer finished (exit code !NSIS_RC!) >> "%LOGFILE%"

if !NSIS_RC! NEQ 0 (
    echo [%DATE% %TIME%] [ERROR] NSIS installer failed with code !NSIS_RC! >> "%LOGFILE%"
    echo [tune-update][ERROR] NSIS installer failed. See _update.log
    del /Q "_update_pending" 2>nul
    exit /b 1
)

REM Verify the new binary is in place.
if not exist "tune-server.exe" (
    echo [%DATE% %TIME%] [ERROR] tune-server.exe missing after install >> "%LOGFILE%"
    del /Q "_update_pending" 2>nul
    exit /b 1
)

REM Cleanup: remove sentinel + downloaded installer.
del /Q "_update_pending" 2>nul
del /Q "{installer_path}" 2>nul

echo [%DATE% %TIME%] Update applied successfully. Relaunching... >> "%LOGFILE%"
if exist "start-tune-server.bat" (
    start "" "{install_dir}\\start-tune-server.bat"
) else (
    start "" "{install_dir}\\tune-server.exe"
)

endlocal
exit /b 0
"""
        helper = exe_dir / "_apply_update.bat"
        helper.write_text(helper_script, encoding="ascii")

        try:
            # DETACHED_PROCESS (0x08) | CREATE_NEW_PROCESS_GROUP (0x200)
            CREATE_FLAGS = 0x00000008 | 0x00000200
            subprocess.Popen(
                ["cmd.exe", "/c", str(helper)],
                cwd=install_dir,
                creationflags=CREATE_FLAGS,
                close_fds=True,
            )
            logger.info("update_helper_spawned", path=str(helper))
        except Exception:
            logger.exception("update_helper_spawn_failed")
            # Clean up sentinel if we failed to spawn the helper
            sentinel.unlink(missing_ok=True)

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

        except Exception as exc:
            logger.warning("update_check_error", error=repr(exc), error_type=type(exc).__name__)
            return None

    async def download_and_install(self) -> bool:
        """Download the update and install it. Returns True on success."""
        if not self._download_url or not self._asset_name:
            return False

        if self.is_source_install:
            logger.warning("update_refused_source_install", version=self._latest_version)
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

            # Windows: the downloaded asset is setup.exe, not a zip.
            # Skip extraction and hand off to the NSIS installer.
            if platform.system().lower() == "windows":
                self._windows_installer_path = archive_path
                self._tmp_dir = tmp_dir
                if self._event_bus:
                    from tune_server.event_bus import Event, EventType
                    await self._event_bus.emit(Event(
                        type=EventType.SYSTEM_UPDATE_INSTALLED,
                        data={
                            "version": self._latest_version,
                            "restart_required": True,
                            "windows_staged": True,
                        },
                    ))
                logger.info("update_ready_windows", version=self._latest_version,
                            installer=str(archive_path))
                return True

            # Linux/macOS: extract the archive and replace files in-place
            extract_dir = tmp_dir / "extracted"
            if archive_path.suffix == ".zip":
                with zipfile.ZipFile(archive_path) as zf:
                    zf.extractall(extract_dir)
            elif archive_path.name.endswith(".tar.gz"):
                with tarfile.open(archive_path, "r:gz") as tf:
                    tf.extractall(extract_dir)

            extracted_dirs = list(extract_dir.iterdir())
            source_dir = extracted_dirs[0] if len(extracted_dirs) == 1 and extracted_dirs[0].is_dir() else extract_dir

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

    def _stage_windows_update(self, exe_dir: Path, source_dir: Path) -> None:
        """Copy the new bundle into _update_staging/ and write the swap
        helper bat. Called from download_and_install on Windows."""
        staging = exe_dir / "_update_staging"
        if staging.exists():
            shutil.rmtree(staging)
        # shutil.copytree fully populates staging; we use copy rather than
        # move so the temp dir stays clean for the GC step in the caller.
        shutil.copytree(str(source_dir), str(staging))

        helper = exe_dir / "_apply_update.bat"
        helper.write_text(_WINDOWS_APPLY_UPDATE_BAT, encoding="ascii")
        logger.info("update_staged", staging=str(staging), helper=str(helper))

    @staticmethod
    def _is_newer(new_version: str, current_version: str) -> bool:
        """Compare semver strings."""
        try:
            new_parts = [int(x) for x in new_version.split(".")]
            cur_parts = [int(x) for x in current_version.split(".")]
            return new_parts > cur_parts
        except (ValueError, AttributeError):
            return False

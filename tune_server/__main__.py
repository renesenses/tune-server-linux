from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path


def _raise_nofile_limit() -> None:
    """Raise the open-file descriptor limit on macOS/Linux.

    The default macOS soft limit is 256, which is too low for Tune
    (SSDP + mDNS + HTTP streamer + WebSocket + file watcher can easily
    exceed it). This is a Python-level safety net in case the shell
    wrapper didn't raise ulimit (e.g. invoked via ``python -m tune_server``).
    """
    if sys.platform == "win32":
        return
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        desired = 4096
        if soft < desired:
            new_soft = min(desired, hard)
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
    except Exception:
        # Non-fatal — the shell wrapper should have already raised it.
        pass


def _ensure_ssl_certs() -> None:
    """Point SSL stack at the bundled certifi CA store.

    Inside a PyInstaller bundle, aiohttp's default SSL context can't
    find OS-level CA roots and silently fails every HTTPS request
    (update check, MusicBrainz, Last.fm, Tidal, etc.). Setting
    SSL_CERT_FILE / REQUESTS_CA_BUNDLE before any TLS happens routes
    every library through certifi's bundled bundle. (Diagnosed on
    macOS DMG v0.7.43: /update/check returned null with no log
    visible at INFO level.)
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        import certifi
        ca = certifi.where()
        os.environ.setdefault("SSL_CERT_FILE", ca)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", ca)
    except Exception:
        pass


def _ensure_data_dir() -> None:
    """Ensure user data directory exists and chdir to it.

    On Windows (PyInstaller): uses %APPDATA%/TuneServer/ for persistent data
    (DB, .env, artwork_cache) separate from the exe directory.
    On first run, migrates existing data from exe directory if found.
    On Linux/macOS: uses exe directory (backward compatible).
    """
    if not getattr(sys, "frozen", False):
        return

    exe_dir = Path(sys.executable).resolve().parent

    if sys.platform == "win32":
        # Windows: store data in %APPDATA%/TuneServer/
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        data_dir = Path(appdata) / "TuneServer"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Migrate existing data from exe directory on first run
        for name in ["tune_server.db", ".env", "artwork_cache", "backups"]:
            src = exe_dir / name
            dst = data_dir / name
            if src.exists() and not dst.exists():
                import shutil
                if src.is_dir():
                    shutil.copytree(str(src), str(dst))
                else:
                    shutil.copy2(str(src), str(dst))
                print(f"[Tune] Migrated {name} to {data_dir}")

        # Copy .env.example if no .env exists
        if not (data_dir / ".env").exists() and (exe_dir / ".env.example").exists():
            import shutil
            shutil.copy2(str(exe_dir / ".env.example"), str(data_dir / ".env"))
            print(f"[Tune] Created .env from .env.example in {data_dir}")

        os.chdir(data_dir)
        # Set TUNE_WEB_DIR to find web assets in exe directory
        if "TUNE_WEB_DIR" not in os.environ:
            for candidate in [exe_dir / "web", exe_dir / "_internal" / "web"]:
                if candidate.is_dir():
                    os.environ["TUNE_WEB_DIR"] = str(candidate)
                    break
        print(f"[Tune] Data directory: {data_dir}")
    else:
        # Linux/macOS: use exe directory
        os.chdir(exe_dir)


def _fix_noconsole_streams() -> None:
    """Fix sys.stdout/stderr being None when running with PyInstaller --noconsole on Windows."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")


def main() -> None:
    _raise_nofile_limit()
    _fix_noconsole_streams()
    _ensure_ssl_certs()
    _ensure_data_dir()

    # Handle subcommands
    if len(sys.argv) > 1 and sys.argv[1] == "migrate-db":
        sys.argv = [sys.argv[0]] + sys.argv[2:]  # Strip subcommand for argparse
        from tune_server.db.migrate import main as migrate_main
        migrate_main()
        return

    from tune_server.app import run_server

    async def _run() -> None:
        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        # add_signal_handler is not supported on Windows
        if sys.platform != "win32":
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, shutdown_event.set)
        await run_server(shutdown_event)

    asyncio.run(_run())


if __name__ == "__main__":
    main()

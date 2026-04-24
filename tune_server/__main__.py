from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path


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
    _fix_noconsole_streams()
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

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path


def _ensure_data_dir() -> None:
    """When running as a PyInstaller binary, chdir to the exe directory
    so that .env, tune_server.db, artwork_cache/ resolve next to the exe."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        os.chdir(exe_dir)


def main() -> None:
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

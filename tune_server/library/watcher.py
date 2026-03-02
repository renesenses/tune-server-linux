from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from tune_server.config import settings
from tune_server.db.engine import Database
from tune_server.db.repository import TrackRepo
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.library.metadata_reader import SUPPORTED_EXTENSIONS
from tune_server.library.scanner import LibraryScanner

logger = structlog.get_logger()


class FileSystemWatcher:
    def __init__(
        self,
        music_dirs: list[str],
        scanner: LibraryScanner,
        db: Database,
        event_bus: EventBus,
    ) -> None:
        self._music_dirs = music_dirs
        self._scanner = scanner
        self._db = db
        self._event_bus = event_bus
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._watch())
        logger.info("filesystem_watcher_started", dirs=self._music_dirs)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("filesystem_watcher_stopped")

    async def update_dirs(self, music_dirs: list[str]) -> None:
        await self.stop()
        self._music_dirs = music_dirs
        await self.start()

    async def _watch(self) -> None:
        try:
            from watchfiles import awatch, Change

            paths = [p for p in self._music_dirs if Path(p).exists()]
            if not paths:
                logger.warning("no_valid_watch_paths")
                return

            track_repo = TrackRepo(self._db)
            pending: dict[str, Change] = {}  # path -> most recent change type
            debounce_task: asyncio.Task | None = None

            async def _flush_pending():
                await asyncio.sleep(settings.watcher_debounce_seconds)
                to_process = dict(pending)
                pending.clear()
                for path_str, change_type in to_process.items():
                    try:
                        if change_type == Change.deleted:
                            await track_repo.delete_by_path(path_str)
                            await self._event_bus.emit(Event(
                                type=EventType.LIBRARY_TRACK_REMOVED,
                                data={"file_path": path_str},
                                source="watcher",
                            ))
                        else:  # added or modified
                            await self._scanner.scan_single(path_str)
                    except Exception:
                        logger.exception("watcher_process_error", path=path_str)

            async for changes in awatch(*paths):
                for change_type, path_str in changes:
                    if Path(path_str).suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    pending[path_str] = change_type

                # Reset debounce timer
                if debounce_task and not debounce_task.done():
                    debounce_task.cancel()
                debounce_task = asyncio.create_task(_flush_pending())

        except ImportError:
            logger.warning("watchfiles_not_installed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("filesystem_watcher_error")

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from tune_server.db.engine import Database
from tune_server.db.repository import AlbumRepo, ArtistRepo, TrackRepo
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.library.artwork import get_album_artwork
from tune_server.library.metadata_reader import SUPPORTED_EXTENSIONS, read_metadata
from tune_server.models import Track

logger = structlog.get_logger()


class LibraryScanner:
    def __init__(self, db: Database, event_bus: EventBus) -> None:
        self._db = db
        self._event_bus = event_bus
        self._artist_repo = ArtistRepo(db)
        self._album_repo = AlbumRepo(db)
        self._track_repo = TrackRepo(db)
        self._scanning = False
        self._lock = asyncio.Lock()

    @property
    def is_scanning(self) -> bool:
        return self._scanning

    async def scan(self, music_dirs: list[str]) -> dict:
        if self._scanning:
            logger.warning("scan_already_in_progress")
            return {"status": "already_scanning"}

        self._scanning = True
        await self._event_bus.emit(Event(
            type=EventType.LIBRARY_SCAN_STARTED,
            source="scanner",
        ))

        stats = {"scanned": 0, "added": 0, "updated": 0, "removed": 0, "errors": 0}

        try:
            async with self._lock:
                existing_paths = await self._track_repo.get_all_paths()

            found_paths: set[str] = set()

            for music_dir in music_dirs:
                dir_path = Path(music_dir)
                if not dir_path.exists():
                    logger.warning("music_dir_not_found", path=music_dir)
                    continue

                logger.info("scanning_directory", path=music_dir)

                for file_path in dir_path.rglob("*"):
                    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    if not file_path.is_file():
                        continue

                    path_str = str(file_path)
                    found_paths.add(path_str)
                    stats["scanned"] += 1

                    try:
                        async with self._lock:
                            if path_str in existing_paths:
                                # Check if file was modified since last scan
                                file_mtime = file_path.stat().st_mtime
                                stored_mtime = await self._track_repo.get_mtime(path_str)
                                if stored_mtime and file_mtime <= stored_mtime:
                                    continue  # File unchanged
                                # File modified — re-process (no lock, already held)
                                added = await self._rescan_file(path_str)
                                if added:
                                    stats["updated"] += 1
                                continue

                            added = await self._process_file(path_str)
                            if added:
                                stats["added"] += 1
                    except Exception:
                        logger.exception("scan_file_error", path=path_str)
                        stats["errors"] += 1

                    # Yield control periodically
                    if stats["scanned"] % 100 == 0:
                        await asyncio.sleep(0)
                        await self._event_bus.emit(Event(
                            type=EventType.LIBRARY_SCAN_PROGRESS,
                            data=dict(stats),
                            source="scanner",
                        ))

            # Re-fetch AFTER scan to avoid deleting watcher-added files
            async with self._lock:
                current_paths = await self._track_repo.get_all_paths()
                removed_paths = current_paths - found_paths
                for path_str in removed_paths:
                    await self._track_repo.delete_by_path(path_str)
                    stats["removed"] += 1

            logger.info("scan_completed", **stats)

        finally:
            self._scanning = False
            await self._event_bus.emit(Event(
                type=EventType.LIBRARY_SCAN_COMPLETED,
                data=dict(stats),
                source="scanner",
            ))

        return stats

    async def _process_file(self, file_path: str) -> bool:
        metadata = await asyncio.to_thread(read_metadata, file_path)
        if metadata is None:
            return False

        # Get or create artist
        artist_name = metadata.album_artist or metadata.artist
        artist = await self._artist_repo.get_or_create(artist_name)

        # Get or create album
        album = await self._album_repo.get_or_create(
            title=metadata.album,
            artist_id=artist.id,
            year=metadata.year,
            genre=metadata.genre,
        )

        # Extract cover art if album doesn't have one yet
        if not album.cover_path:
            cover_path = await asyncio.to_thread(get_album_artwork, file_path)
            if cover_path:
                album.cover_path = cover_path
                await self._album_repo.update(album)

        # Create track
        track = Track(
            title=metadata.title,
            album_id=album.id,
            artist_id=artist.id,
            disc_number=metadata.disc_number,
            track_number=metadata.track_number,
            duration_ms=metadata.duration_ms,
            file_path=file_path,
            format=metadata.format,
            sample_rate=metadata.sample_rate,
            bit_depth=metadata.bit_depth,
            channels=metadata.channels,
        )
        track_id = await self._track_repo.create(track)

        # Store file mtime for incremental scanning
        mtime = Path(file_path).stat().st_mtime
        await self._track_repo.update_mtime(file_path, mtime)

        # Update album track count
        await self._album_repo.update_track_count(album.id)

        await self._event_bus.emit(Event(
            type=EventType.LIBRARY_TRACK_ADDED,
            data={"track_id": track_id, "file_path": file_path},
            source="scanner",
        ))

        return True

    async def _rescan_file(self, file_path: str) -> bool:
        """Re-process a file (delete existing + re-add). Caller must hold self._lock."""
        existing = await self._track_repo.get_by_path(file_path)
        if existing:
            await self._track_repo.delete(existing.id)
        return await self._process_file(file_path)

    async def scan_single(self, file_path: str) -> bool:
        async with self._lock:
            return await self._rescan_file(file_path)

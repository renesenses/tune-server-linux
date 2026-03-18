from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import structlog

from tune_server.db.engine import Database
from tune_server.db.repository import AlbumRepo, ArtistRepo, TrackRepo
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.library.artwork import fetch_cover_from_musicbrainz, get_album_artwork
from tune_server.library.metadata_reader import SUPPORTED_EXTENSIONS, read_metadata
from tune_server.config import settings
from tune_server.models import Track

logger = structlog.get_logger()


AUDIO_HASH_SAMPLE_SIZE = 64 * 1024  # 64KB sample for audio hash


def compute_audio_hash(file_path: str) -> str | None:
    """Compute MD5 hash of a 64KB audio sample from the middle of the file.

    Skips metadata (typically at start/end) by reading from 25% into the file.
    Returns None if file cannot be read.
    """
    try:
        path = Path(file_path)
        file_size = path.stat().st_size
        offset = file_size // 4  # Start at 25% to skip metadata headers

        md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            f.seek(offset)
            data = f.read(AUDIO_HASH_SAMPLE_SIZE)
            md5.update(data)
        return md5.hexdigest()
    except (PermissionError, OSError):
        return None


def _list_audio_files(dir_path: Path) -> list[Path]:
    """List audio files in a directory tree (blocking, runs in thread)."""
    try:
        return [
            f for f in dir_path.rglob("*")
            if f.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
    except PermissionError:
        return []


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

                # List files in a thread to avoid blocking the event loop on NAS
                file_list = await asyncio.to_thread(_list_audio_files, dir_path)

                for file_path in file_list:
                    try:
                        if not file_path.is_file():
                            continue
                    except PermissionError:
                        continue

                    path_str = str(file_path).replace("\\", "/")
                    found_paths.add(path_str)
                    stats["scanned"] += 1

                    try:
                        if path_str in existing_paths:
                            await self._handle_existing_file(path_str, file_path, stats)
                        else:
                            added = await self._process_new_file(path_str)
                            if added:
                                stats["added"] += 1
                    except Exception:
                        logger.exception("scan_file_error", path=path_str)
                        stats["errors"] += 1

                    # Yield control frequently to keep playback responsive
                    if stats["scanned"] % 10 == 0:
                        await asyncio.sleep(0)
                    if stats["scanned"] % 100 == 0:
                        await self._event_bus.emit(Event(
                            type=EventType.LIBRARY_SCAN_PROGRESS,
                            data=dict(stats),
                            source="scanner",
                        ))

            # Remove tracks that no longer exist on disk
            async with self._lock:
                current_paths = await self._track_repo.get_all_paths()
                scanned_roots = [str(Path(d).resolve()).replace("\\", "/") for d in music_dirs]
                candidate_paths = {
                    p for p in current_paths
                    if any(p.replace("\\", "/").startswith(root + "/") for root in scanned_roots)
                }
                removed_paths = candidate_paths - found_paths
                for path_str in removed_paths:
                    await self._track_repo.delete_by_path(path_str)
                    stats["removed"] += 1

            # Remove duplicate tracks (same album + disc + track from different mount points)
            deduped = await self._track_repo.deduplicate()
            if deduped:
                logger.info("deduplicated_tracks", count=deduped)

            # Clean up orphan albums (no tracks)
            orphans = await self._album_repo.delete_orphans()
            if orphans:
                logger.info("deleted_orphan_albums", count=orphans)

            logger.info("scan_completed", **stats)

        finally:
            self._scanning = False
            await self._event_bus.emit(Event(
                type=EventType.LIBRARY_SCAN_COMPLETED,
                data=dict(stats),
                source="scanner",
            ))

        return stats

    async def _handle_existing_file(self, path_str: str, file_path: Path, stats: dict) -> None:
        """Handle a file that already exists in the database."""
        # Backfill audio_hash if missing (quick DB check under lock)
        async with self._lock:
            row = await self._db.fetchone(
                "SELECT audio_hash FROM tracks WHERE file_path = ?", (path_str,)
            )
        if row and not row["audio_hash"]:
            # Heavy I/O outside the lock
            h = await asyncio.to_thread(compute_audio_hash, path_str)
            if h:
                async with self._lock:
                    await self._track_repo.update_audio_hash(path_str, h)

        # Check if file was modified since last scan
        try:
            file_mtime = file_path.stat().st_mtime
        except OSError:
            return
        async with self._lock:
            stored_mtime = await self._track_repo.get_mtime(path_str)
        if stored_mtime and file_mtime <= stored_mtime:
            return  # File unchanged

        # File modified — re-process
        added = await self._rescan_file(path_str)
        if added:
            stats["updated"] += 1

    async def _process_new_file(self, file_path: str) -> bool:
        """Process a new file: read metadata & artwork outside lock, write DB under lock."""
        # --- Heavy I/O: NO lock held ---
        metadata = await asyncio.to_thread(read_metadata, file_path)
        if metadata is None:
            return False

        audio_hash = await asyncio.to_thread(compute_audio_hash, file_path)
        if audio_hash is None:
            return False

        # --- DB operations: short lock ---
        async with self._lock:
            # Skip if identical audio content already exists
            existing = await self._track_repo.find_by_audio_hash(audio_hash)
            if existing:
                return False

            # Get or create artist
            artist_name = metadata.album_artist or metadata.artist
            artist = await self._artist_repo.get_or_create(artist_name)

            # Get or create album
            if metadata.album_artist:
                album = await self._album_repo.get_or_create(
                    title=metadata.album,
                    artist_id=artist.id,
                    year=metadata.year,
                    genre=metadata.genre,
                )
            else:
                album = await self._album_repo.get_by_title(metadata.album)
                if not album:
                    album = await self._album_repo.get_or_create(
                        title=metadata.album,
                        artist_id=artist.id,
                        year=metadata.year,
                        genre=metadata.genre,
                    )

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

            # Store file mtime and audio hash
            mtime = Path(file_path).stat().st_mtime
            await self._track_repo.update_mtime(file_path, mtime)
            await self._track_repo.update_audio_hash(file_path, audio_hash)

            # Update album track count
            await self._album_repo.update_track_count(album.id)

        # --- Artwork: NO lock held (can be slow, especially MusicBrainz) ---
        async with self._lock:
            album_cover = album.cover_path
        if not album_cover:
            cover_path = await asyncio.to_thread(get_album_artwork, file_path)
            if not cover_path:
                cover_path = await asyncio.to_thread(
                    fetch_cover_from_musicbrainz,
                    metadata.album_artist or metadata.artist,
                    metadata.album,
                    settings.artwork_cache_dir,
                )
            if cover_path:
                async with self._lock:
                    album.cover_path = cover_path
                    await self._album_repo.update(album)

        await self._event_bus.emit(Event(
            type=EventType.LIBRARY_TRACK_ADDED,
            data={"track_id": track_id, "file_path": file_path},
            source="scanner",
        ))

        return True

    async def _rescan_file(self, file_path: str) -> bool:
        """Re-process a file (delete existing + re-add)."""
        async with self._lock:
            existing = await self._track_repo.get_by_path(file_path)
            if existing:
                await self._track_repo.delete(existing.id)
        return await self._process_new_file(file_path)

    async def scan_single(self, file_path: str) -> bool:
        return await self._rescan_file(file_path)

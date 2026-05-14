from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import structlog

from tune_server.db.engine import Database
from tune_server.db.repository import AlbumRepo, ArtistRepo, TrackCreditRepo, TrackRepo
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.library.artwork import fetch_cover_from_musicbrainz, get_album_artwork
from tune_server.library.metadata_reader import SUPPORTED_EXTENSIONS, read_metadata
from tune_server.config import settings
from tune_server.models import Track, TrackCredit

logger = structlog.get_logger()


AUDIO_HASH_SAMPLE_SIZE = 64 * 1024  # 64KB sample for audio hash


def _quality_suffix(sample_rate: int | None, bit_depth: int | None) -> str:
    """Return a human-readable quality suffix like '96kHz/24bit'."""
    if not sample_rate:
        return ""
    sr = sample_rate / 1000  # e.g. 44.1, 48, 96, 192
    sr_str = f"{sr:g}kHz"
    if bit_depth and bit_depth > 16:
        return f"{sr_str}/{bit_depth}bit"
    return sr_str


def _same_quality_tier(sr1: int | None, sr2: int | None) -> bool:
    """Check if two sample rates belong to the same quality tier."""
    if sr1 is None or sr2 is None:
        return True  # Can't compare — assume same
    return sr1 == sr2


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
    except (PermissionError, OSError) as e:
        logger.debug("audio_hash_error", path=str(file_path), error=str(e))
        return None


# Directories to exclude from scanning
SKIP_DIRS = {"duplicates", ".tune", ".Spotlight-V100", ".Trashes", "@eaDir", "#recycle", ".DS_Store"}


def _list_audio_files(dir_path: Path) -> list[Path]:
    """List audio files in a directory tree (blocking, runs in thread)."""
    try:
        return [
            f for f in dir_path.rglob("*")
            if f.suffix.lower() in SUPPORTED_EXTENSIONS
            and not any(skip in f.parts for skip in SKIP_DIRS)
            and not f.name.startswith("._")
        ]
    except PermissionError as e:
        logger.debug("list_audio_files_permission_denied", path=str(dir_path), error=str(e))
        return []


class LibraryScanner:
    def __init__(self, db: Database, event_bus: EventBus,
                 credit_repo: TrackCreditRepo | None = None) -> None:
        self._db = db
        self._event_bus = event_bus
        self._artist_repo = ArtistRepo(db)
        self._album_repo = AlbumRepo(db)
        self._track_repo = TrackRepo(db)
        self._credit_repo = credit_repo
        self._scanning = False
        self._lock = asyncio.Lock()
        # Snapshot of the last completed scan, for diagnostics.
        self.last_scan_stats: dict | None = None
        self.last_scan_at: str | None = None

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
                    except PermissionError as e:
                        logger.debug("scan_file_permission_denied", path=str(file_path), error=str(e))
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
            from datetime import datetime, timezone
            self.last_scan_stats = dict(stats)
            self.last_scan_at = datetime.now(timezone.utc).isoformat()

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
        except OSError as e:
            logger.debug("scan_file_stat_error", path=str(file_path), error=str(e))
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

            # Get or create artist — fallback to folder name if tags are empty
            artist_name = metadata.album_artist or metadata.artist
            if not artist_name:
                # Extract from file path: .../Artist/Album/track.ext
                parts = file_path.replace("\\", "/").split("/")
                _skip = {"AA_FLAC","AA_WAV","AA_MP3","AA_DSD","JAZZ","CLASSICAL","ROCK",
                         "POP","BLUES","SOUL","ELECTRO","WORLD","CHANSON","COMPILATION",
                         "music","recordings","data","Qobuz","Tidal","Local","Deezer",
                         "Spotify","YouTube","Unknown Artist","V_DSF"}
                for idx in [-3, -4]:
                    if len(parts) >= abs(idx) + 1 and parts[idx] not in _skip and not parts[idx].startswith("AA_"):
                        artist_name = parts[idx]
                        break
                if not artist_name:
                    artist_name = "Unknown Artist"
                logger.debug("artist_from_path", path=file_path, artist=artist_name)
            artist_mbid = metadata.musicbrainz_album_artist_id or metadata.musicbrainz_artist_id
            artist = await self._artist_repo.get_or_create(
                artist_name, musicbrainz_id=artist_mbid,
                sort_name=metadata.album_artist_sort,
            )

            # Get or create album — separate albums when sample rates differ
            # or when MusicBrainz Release IDs differ
            base_title = metadata.album
            if not base_title:
                # Fallback to folder name
                parts = file_path.replace("\\", "/").split("/")
                base_title = parts[-2] if len(parts) >= 3 else "Unknown Album"
                logger.debug("album_from_path", path=file_path, album=base_title)

            mb_release_id = metadata.musicbrainz_release_id
            mb_release_group_id = metadata.musicbrainz_release_group_id
            album_kwargs = dict(
                year=metadata.year, original_year=metadata.original_year,
                release_date=metadata.release_date, original_date=metadata.original_date,
                genre=metadata.genre,
                label=metadata.label, catalog_number=metadata.catalog_number,
                musicbrainz_release_id=mb_release_id,
                musicbrainz_release_group_id=mb_release_group_id,
            )

            # MBID-first lookup: authoritative discriminant
            album = None
            if mb_release_id:
                album = await self._album_repo.get_by_musicbrainz_release_id(mb_release_id)

            if not album:
                if metadata.album_artist:
                    album = await self._album_repo.get_by_title_and_artist(base_title, artist.id)
                else:
                    album = await self._album_repo.get_by_title(base_title)
                # Don't merge into an album that belongs to a different release
                if (album and mb_release_id
                        and album.musicbrainz_release_id
                        and album.musicbrainz_release_id != mb_release_id):
                    logger.info("album_mbid_split", existing=album.musicbrainz_release_id,
                                new=mb_release_id, title=base_title)
                    album = None

            if album:
                # Check if existing album has a different sample rate
                dominant_sr = await self._album_repo.get_dominant_sample_rate(album.id)
                if not _same_quality_tier(dominant_sr, metadata.sample_rate):
                    # Different quality — create a suffixed album for the new track
                    suffix = _quality_suffix(metadata.sample_rate, metadata.bit_depth)
                    qualified_title = f"{base_title} ({suffix})" if suffix else base_title
                    split_album = await self._album_repo.get_by_title(qualified_title)
                    if split_album:
                        album = split_album
                    else:
                        album = await self._album_repo.get_or_create(
                            title=qualified_title,
                            artist_id=album.artist_id or artist.id,
                            **album_kwargs,
                        )
                    logger.info("album_quality_split", base=base_title, new_title=qualified_title)
                # Backfill label/catalog_number if the album was created
                # earlier from a track that didn't carry these tags.
                if metadata.label or metadata.catalog_number:
                    await self._album_repo.update_label(
                        album.id, metadata.label, metadata.catalog_number,
                    )
                # Backfill MusicBrainz IDs
                if mb_release_id or mb_release_group_id:
                    await self._album_repo.update_musicbrainz_ids(
                        album.id, mb_release_id, mb_release_group_id,
                    )
            else:
                album = await self._album_repo.get_or_create(
                    title=base_title,
                    artist_id=artist.id,
                    **album_kwargs,
                )

            # Create track
            track = Track(
                title=metadata.title or Path(file_path).stem,
                album_id=album.id,
                artist_id=artist.id,
                artist_name=metadata.artist or artist_name,
                album_title=base_title,
                disc_number=metadata.disc_number,
                disc_subtitle=metadata.disc_subtitle,
                track_number=metadata.track_number,
                duration_ms=metadata.duration_ms,
                file_path=file_path,
                format=metadata.format,
                sample_rate=metadata.sample_rate,
                bit_depth=metadata.bit_depth,
                channels=metadata.channels,
                musicbrainz_recording_id=metadata.musicbrainz_recording_id,
            )
            track_id = await self._track_repo.create(track)

            # Store file mtime and audio hash
            mtime = Path(file_path).stat().st_mtime
            await self._track_repo.update_mtime(file_path, mtime)
            await self._track_repo.update_audio_hash(file_path, audio_hash)

            # Update album track count + quality stats
            await self._album_repo.update_track_count(album.id)
            await self._album_repo.refresh_quality(album.id)

            # Save track credits
            if self._credit_repo and metadata.credits:
                try:
                    credit_models: list[TrackCredit] = []
                    for pos, cred in enumerate(metadata.credits):
                        cred_name = cred.get("name", "").strip()
                        if not cred_name:
                            continue
                        cred_artist = await self._artist_repo.get_or_create(cred_name)
                        credit_models.append(TrackCredit(
                            track_id=track_id,
                            artist_id=cred_artist.id,
                            artist_name=cred_name,
                            role=cred.get("role", "performer"),
                            instrument=cred.get("instrument"),
                            position=pos,
                        ))
                    if credit_models:
                        await self._credit_repo.add_many(credit_models)
                except Exception:
                    logger.warning("credit_save_error", track_id=track_id, path=file_path, exc_info=True)

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
        if self._scanning:
            return False
        return await self._rescan_file(file_path)

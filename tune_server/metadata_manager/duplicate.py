"""Duplicate detection — find true duplicates by audio MD5 hash."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Skip first N bytes (header) and read a chunk for audio-only hash
HEADER_SKIP = 8192  # Skip file headers (ID3, FLAC streaminfo, etc.)
CHUNK_SIZE = 1024 * 1024  # Read 1MB of audio data for hash


async def compute_audio_hash(file_path: str) -> str | None:
    """Compute MD5 hash of the audio portion of a file.

    Skips file headers to compare only audio content,
    detecting true duplicates even with different tags.
    """
    path = Path(file_path)
    if not path.exists():
        return None

    try:
        def _hash():
            md5 = hashlib.md5()
            with open(path, "rb") as f:
                f.seek(HEADER_SKIP)
                data = f.read(CHUNK_SIZE)
                if not data:
                    return None
                md5.update(data)
            return md5.hexdigest()

        return await asyncio.to_thread(_hash)
    except Exception:
        return None


async def scan_duplicates(
    db,
    on_progress=None,
    limit: int = 5000,
) -> dict:
    """Scan library for duplicate tracks by audio hash.

    Returns: {total_scanned, duplicates_found, groups: [{hash, tracks: [...]}]}
    """
    rows = await db.fetchall(
        """SELECT id, file_path, title, artist_name, audio_hash
           FROM tracks
           WHERE source = 'local' AND file_path IS NOT NULL
           LIMIT ?""",
        (limit,),
    )

    total = len(rows)
    hash_map: dict[str, list[dict]] = {}
    scanned = 0
    errors = 0

    for i, row in enumerate(rows):
        file_path = row["file_path"]
        existing_hash = row.get("audio_hash") if "audio_hash" in row.keys() else None

        if existing_hash:
            h = existing_hash
        else:
            h = await compute_audio_hash(file_path)
            if h:
                # Store hash for future scans
                await db.execute(
                    "UPDATE tracks SET audio_hash = ? WHERE id = ?",
                    (h, row["id"]),
                )

        if h:
            entry = {
                "id": row["id"],
                "title": row["title"],
                "artist_name": row["artist_name"],
                "file_path": file_path,
            }
            hash_map.setdefault(h, []).append(entry)
            scanned += 1
        else:
            errors += 1

        if on_progress and (i + 1) % 100 == 0:
            await on_progress(i + 1, total)

    await db.commit()

    # Find groups with >1 track (actual duplicates)
    duplicate_groups = []
    for h, tracks in hash_map.items():
        if len(tracks) > 1:
            duplicate_groups.append({"hash": h, "tracks": tracks})

            # Store in duplicate_tracks table
            for j in range(1, len(tracks)):
                await db.execute(
                    """INSERT OR IGNORE INTO duplicate_tracks
                       (track_id_a, track_id_b, audio_hash)
                       VALUES (?, ?, ?)""",
                    (tracks[0]["id"], tracks[j]["id"], h),
                )

    await db.commit()

    duplicates_found = sum(len(g["tracks"]) - 1 for g in duplicate_groups)

    logger.info("duplicate_scan_complete",
                scanned=scanned, groups=len(duplicate_groups),
                duplicates=duplicates_found, errors=errors)

    return {
        "total_scanned": scanned,
        "duplicates_found": duplicates_found,
        "groups": duplicate_groups,
        "errors": errors,
    }

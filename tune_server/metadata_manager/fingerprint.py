"""Audio fingerprinting — Chromaprint/fpcalc + AcoustID lookup.

Identifies unknown tracks by their audio fingerprint.
Requires: fpcalc binary (Chromaprint) installed on the system.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import structlog

logger = structlog.get_logger()

ACOUSTID_API = "https://api.acoustid.org/v2/lookup"
ACOUSTID_CLIENT = "8XaBELgH"  # Free AcoustID API key for open source projects


async def compute_fingerprint(file_path: str) -> tuple[int, str] | None:
    """Compute audio fingerprint using fpcalc (Chromaprint CLI).

    Returns (duration_seconds, fingerprint_string) or None if failed.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "fpcalc", "-json", file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            logger.debug("fpcalc_error", file=file_path, stderr=stderr.decode()[:100])
            return None

        data = json.loads(stdout.decode())
        duration = int(data.get("duration", 0))
        fingerprint = data.get("fingerprint", "")

        if not fingerprint:
            return None

        return duration, fingerprint

    except FileNotFoundError:
        logger.warning("fpcalc_not_installed", hint="Install chromaprint: apt install libchromaprint-tools / brew install chromaprint")
        return None
    except asyncio.TimeoutError:
        logger.debug("fpcalc_timeout", file=file_path)
        return None
    except Exception:
        logger.debug("fpcalc_exception", file=file_path)
        return None


async def lookup_acoustid(
    duration: int,
    fingerprint: str,
    api_key: str = ACOUSTID_CLIENT,
) -> list[dict]:
    """Look up a fingerprint on AcoustID.

    Returns list of candidates with:
    - musicbrainz_recording_id
    - title, artist
    - score (0.0-1.0)
    """
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            params = {
                "client": api_key,
                "duration": str(duration),
                "fingerprint": fingerprint,
                "meta": "recordings+releasegroups",
            }
            async with session.post(ACOUSTID_API, data=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

        results = []
        for result in data.get("results", []):
            score = result.get("score", 0)
            acoustid = result.get("id", "")

            for recording in result.get("recordings", []):
                title = recording.get("title", "")
                artists = recording.get("artists", [])
                artist = " ".join(a.get("name", "") for a in artists) if artists else ""

                # Get album from release groups
                album = ""
                release_groups = recording.get("releasegroups", [])
                if release_groups:
                    album = release_groups[0].get("title", "")

                results.append({
                    "acoustid": acoustid,
                    "musicbrainz_recording_id": recording.get("id", ""),
                    "title": title,
                    "artist_name": artist,
                    "album_title": album,
                    "score": score,
                })

        return results

    except Exception:
        logger.exception("acoustid_lookup_error")
        return []


async def identify_track(file_path: str) -> dict | None:
    """Identify a track by its audio fingerprint.

    Returns best match dict or None.
    """
    fp = await compute_fingerprint(file_path)
    if fp is None:
        return None

    duration, fingerprint = fp
    candidates = await lookup_acoustid(duration, fingerprint)

    if not candidates:
        return None

    # Return best match
    best = max(candidates, key=lambda c: c["score"])
    best["fingerprint_duration"] = duration
    return best


async def identify_batch(
    tracks: list[dict],
    db=None,
    on_progress=None,
) -> dict:
    """Identify multiple tracks by fingerprint.

    Args:
        tracks: List of {id, file_path, title, artist_name}
        db: Database for storing suggestions
        on_progress: async callback(current, total, result)

    Returns: {total, identified, not_found, errors}
    """
    total = len(tracks)
    identified = 0
    not_found = 0
    errors = 0
    results = []

    for i, track in enumerate(tracks):
        file_path = track.get("file_path")
        if not file_path:
            errors += 1
            continue

        try:
            match = await identify_track(file_path)

            if match and match.get("score", 0) > 0.5:
                identified += 1
                result = {
                    "track_id": track["id"],
                    "current_title": track.get("title", ""),
                    "matched_title": match.get("title", ""),
                    "matched_artist": match.get("artist_name", ""),
                    "matched_album": match.get("album_title", ""),
                    "acoustid": match.get("acoustid", ""),
                    "musicbrainz_recording_id": match.get("musicbrainz_recording_id", ""),
                    "score": match.get("score", 0),
                }
                results.append(result)

                # Store as suggestions
                if db:
                    for field in ["title", "artist_name", "album_title", "musicbrainz_recording_id", "acoustid"]:
                        value = match.get(field) or result.get(f"matched_{field}")
                        if value:
                            db_field = field if field != "artist_name" else "artist_name"
                            await db.execute(
                                """INSERT INTO metadata_suggestions
                                   (track_id, field, current_value, suggested_value, source, confidence)
                                   VALUES (?, ?, ?, ?, 'acoustid', ?)""",
                                (track["id"], db_field, track.get(field, ""), value, match["score"]),
                            )
                    await db.commit()
            else:
                not_found += 1

        except Exception:
            errors += 1

        if on_progress:
            await on_progress(i + 1, total, results[-1] if results else None)

        # Rate limit AcoustID (3 req/s max)
        await asyncio.sleep(0.35)

    return {
        "total": total,
        "identified": identified,
        "not_found": not_found,
        "errors": errors,
        "results": results,
    }

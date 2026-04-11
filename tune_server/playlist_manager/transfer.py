"""Transfer orchestration — transfer playlists between services."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import structlog

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.playlist_manager.matcher import find_best_match, MatchResult

logger = structlog.get_logger()


async def execute_transfer(
    source_tracks: list[dict],
    source_service: str,
    source_playlist_name: str,
    source_playlist_id: str,
    target_service: str,
    target_name: str | None,
    search_func,
    db,
    event_bus: EventBus | None = None,
    match_threshold: float = 0.6,
    include_approximate: bool = True,
    dry_run: bool = False,
    create_on_target: bool = False,
    create_playlist_func=None,
    add_tracks_func=None,
) -> dict:
    """Execute a playlist transfer with matching.

    Args:
        source_tracks: List of dicts with title, artist_name, album_title, duration_ms, source_id, isrc
        source_service: Source service name
        source_playlist_name: Name of the source playlist
        source_playlist_id: ID on source service
        target_service: Target service name
        target_name: Name for the target playlist (defaults to source name)
        search_func: async (query, limit) -> list[dict] for the target service
        db: Database instance
        event_bus: For progress events
        match_threshold: Minimum score
        include_approximate: Include fuzzy matches
        dry_run: Preview without creating
        create_on_target: Create playlist on streaming service
        create_playlist_func: async (name) -> playlist_id
        add_tracks_func: async (playlist_id, track_ids) -> count

    Returns:
        Transfer result dict
    """
    playlist_name = target_name or source_playlist_name
    total = len(source_tracks)
    matched = 0
    approximate = 0
    not_found = 0
    track_results = []

    # Record start in history
    transfer_id = None
    if not dry_run and db:
        try:
            await db.execute(
                """INSERT INTO transfer_history
                   (operation, source_service, source_playlist_id, source_playlist_name,
                    target_service, target_playlist_name, total_tracks, status, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'in_progress', ?)""",
                ("transfer", source_service, source_playlist_id, source_playlist_name,
                 target_service, playlist_name, total, datetime.utcnow().isoformat()),
            )
            await db.commit()
            row = await db.fetchone("SELECT last_insert_rowid() as id")
            transfer_id = row["id"] if row else None
        except Exception:
            logger.debug("transfer_history_insert_error")

    # Emit start event
    if event_bus:
        event_bus.emit_nowait(Event(
            type=EventType.PLAYLIST_MANAGER_TRANSFER_STARTED
            if hasattr(EventType, "PLAYLIST_MANAGER_TRANSFER_STARTED")
            else "playlist_manager.transfer.started",
            data={"total": total, "source": source_service, "target": target_service,
                  "playlist_name": playlist_name},
            source="playlist_manager",
        ))

    # Match each track
    matched_target_ids = []
    sem = asyncio.Semaphore(5)  # Rate limit

    async def match_one(i: int, track: dict) -> dict:
        nonlocal matched, approximate, not_found
        async with sem:
            result = await find_best_match(
                source_title=track.get("title", ""),
                source_artist=track.get("artist_name", ""),
                source_album=track.get("album_title", ""),
                source_duration_ms=track.get("duration_ms", 0),
                source_isrc=track.get("isrc", ""),
                search_func=search_func,
                threshold=match_threshold,
            )

            track_result = {
                "title": track.get("title", ""),
                "artist_name": track.get("artist_name", ""),
                "album_title": track.get("album_title", ""),
                "source_id": track.get("source_id", ""),
                "status": result.status,
                "match_method": result.best_match.match_method if result.best_match else "",
                "confidence": result.best_match.confidence if result.best_match else "",
                "score": result.best_match.score if result.best_match else 0.0,
                "target_id": result.best_match.source_id if result.best_match else "",
                "target_title": result.best_match.title if result.best_match else "",
                "target_artist": result.best_match.artist_name if result.best_match else "",
                "alternatives": [
                    {"title": a.title, "artist_name": a.artist_name,
                     "source_id": a.source_id, "score": a.score}
                    for a in result.alternatives
                ],
            }

            if result.status == "matched":
                matched += 1
                if result.best_match:
                    matched_target_ids.append(result.best_match.source_id)
            elif result.status == "approximate":
                approximate += 1
                if include_approximate and result.best_match:
                    matched_target_ids.append(result.best_match.source_id)
            else:
                not_found += 1

            # Progress event
            if event_bus and (i + 1) % 5 == 0:
                event_bus.emit_nowait(Event(
                    type="playlist_manager.transfer.progress",
                    data={"current": i + 1, "total": total, "matched": matched,
                          "approximate": approximate, "not_found": not_found},
                    source="playlist_manager",
                ))

            return track_result

    # Run matching (sequential with semaphore for rate limiting)
    for i, track in enumerate(source_tracks):
        tr = await match_one(i, track)
        track_results.append(tr)

    # Create local playlist (unless dry_run)
    local_playlist_id = None
    target_playlist_id = None

    if not dry_run and db and target_service == "local":
        try:
            await db.execute(
                "INSERT INTO playlists (name, description) VALUES (?, ?)",
                (playlist_name, f"Transferred from {source_service}: {source_playlist_name}"),
            )
            await db.commit()
            row = await db.fetchone("SELECT last_insert_rowid() as id")
            local_playlist_id = row["id"] if row else None
            # Note: track insertion would need source_id → local track mapping
            # For now, we store the transfer result with matched IDs
        except Exception:
            logger.exception("transfer_create_playlist_error")

    # Create on target streaming service
    if not dry_run and create_on_target and create_playlist_func and matched_target_ids:
        try:
            target_playlist_id = await create_playlist_func(playlist_name)
            if target_playlist_id and add_tracks_func:
                await add_tracks_func(target_playlist_id, matched_target_ids)
        except Exception:
            logger.exception("transfer_create_on_target_error")

    # Update history
    if transfer_id and db:
        try:
            await db.execute(
                """UPDATE transfer_history
                   SET matched=?, approximate=?, not_found=?, status='completed',
                       target_playlist_id=?, details=?, completed_at=?
                   WHERE id=?""",
                (matched, approximate, not_found, target_playlist_id or local_playlist_id,
                 json.dumps(track_results), datetime.utcnow().isoformat(), transfer_id),
            )
            await db.commit()
        except Exception:
            pass

    # Emit completion
    if event_bus:
        event_bus.emit_nowait(Event(
            type="playlist_manager.transfer.completed",
            data={"transfer_id": transfer_id, "matched": matched,
                  "approximate": approximate, "not_found": not_found},
            source="playlist_manager",
        ))

    return {
        "transfer_id": transfer_id,
        "source_name": source_playlist_name,
        "target_service": target_service,
        "target_playlist_id": target_playlist_id,
        "local_playlist_id": local_playlist_id,
        "total_tracks": total,
        "matched": matched,
        "approximate": approximate,
        "not_found": not_found,
        "tracks": track_results,
    }

"""Batch operations — transfer ALL playlists, merge playlists."""

from __future__ import annotations

import asyncio
import uuid

import structlog

from tune_server.playlist_manager.transfer import execute_transfer

logger = structlog.get_logger()


async def batch_transfer(
    source_service_name: str,
    target_service_name: str,
    source_service,
    search_func,
    db,
    event_bus=None,
    playlist_ids: list[str] | None = None,
    match_threshold: float = 0.6,
) -> dict:
    """Transfer multiple (or all) playlists from one service to another.

    Args:
        source_service_name: Source service name
        target_service_name: Target service name
        source_service: StreamingService instance (source)
        search_func: async (query, limit) -> list[dict] for target
        db: Database instance
        event_bus: For progress events
        playlist_ids: Specific IDs to transfer, or None for ALL
        match_threshold: Minimum match score

    Returns:
        Batch result dict
    """
    batch_id = str(uuid.uuid4())[:8]

    # Get playlists from source
    try:
        all_playlists = await source_service.get_user_playlists()
    except Exception as e:
        return {"batch_id": batch_id, "error": str(e), "status": "failed"}

    # Filter if specific IDs
    if playlist_ids:
        id_set = set(playlist_ids)
        playlists = [p for p in all_playlists if p.source_id in id_set]
    else:
        playlists = all_playlists

    total = len(playlists)
    results = []

    if event_bus:
        event_bus.emit_nowait(_event("playlist_manager.batch.started", {
            "batch_id": batch_id, "total": total,
            "source": source_service_name, "target": target_service_name,
        }))

    for i, pl in enumerate(playlists):
        try:
            # Get tracks for this playlist
            raw_tracks = await source_service.get_playlist_tracks(pl.source_id)
            source_tracks = [
                {
                    "title": getattr(t, "title", ""),
                    "artist_name": getattr(t, "artist_name", ""),
                    "album_title": getattr(t, "album_title", ""),
                    "duration_ms": getattr(t, "duration_ms", 0),
                    "source_id": getattr(t, "source_id", ""),
                    "isrc": getattr(t, "isrc", ""),
                }
                for t in raw_tracks
            ]

            result = await execute_transfer(
                source_tracks=source_tracks,
                source_service=source_service_name,
                source_playlist_name=pl.name,
                source_playlist_id=pl.source_id,
                target_service=target_service_name,
                target_name=pl.name,
                search_func=search_func,
                db=db,
                event_bus=None,  # Don't emit per-playlist events in batch
                match_threshold=match_threshold,
            )
            results.append({
                "playlist_name": pl.name,
                "total_tracks": result["total_tracks"],
                "matched": result["matched"],
                "not_found": result["not_found"],
            })
        except Exception as e:
            results.append({
                "playlist_name": pl.name,
                "error": str(e),
            })

        if event_bus and (i + 1) % 2 == 0:
            event_bus.emit_nowait(_event("playlist_manager.batch.progress", {
                "batch_id": batch_id, "current": i + 1, "total": total,
                "last_playlist": pl.name,
            }))

        await asyncio.sleep(0.5)  # Rate limit between playlists

    if event_bus:
        event_bus.emit_nowait(_event("playlist_manager.batch.completed", {
            "batch_id": batch_id, "total": total,
            "completed": len([r for r in results if "error" not in r]),
        }))

    return {
        "batch_id": batch_id,
        "total_playlists": total,
        "status": "completed",
        "results": results,
    }


async def merge_playlists(
    playlist_sources: list[dict],
    target_name: str,
    db,
    streaming_manager,
    deduplicate: bool = True,
) -> dict:
    """Merge multiple playlists into one local playlist.

    Args:
        playlist_sources: [{service, playlist_id}, ...]
        target_name: Name for the merged playlist
        db: Database instance
        streaming_manager: StreamingManager for service access
        deduplicate: Remove duplicate tracks

    Returns:
        Merge result dict
    """
    all_tracks = []
    seen_keys = set()

    for src in playlist_sources:
        svc_name = src.get("service", "local")
        pid = src.get("playlist_id", "")

        if svc_name == "local":
            rows = await db.fetchall(
                """SELECT t.title, t.artist_name, t.id as track_id,
                          a.title as album_title, t.duration_ms
                   FROM playlist_tracks pt
                   JOIN tracks t ON t.id = pt.track_id
                   LEFT JOIN albums a ON a.id = t.album_id
                   WHERE pt.playlist_id = ?
                   ORDER BY pt.position""",
                (int(pid),),
            )
            tracks = [dict(r) for r in rows]
        else:
            svc = streaming_manager.service(svc_name)
            if not svc:
                continue
            try:
                raw = await svc.get_playlist_tracks(pid)
                tracks = [
                    {
                        "title": getattr(t, "title", ""),
                        "artist_name": getattr(t, "artist_name", ""),
                        "album_title": getattr(t, "album_title", ""),
                        "track_id": None,
                    }
                    for t in raw
                ]
            except Exception:
                continue

        for t in tracks:
            if deduplicate:
                key = f"{(t.get('title') or '').lower()}|{(t.get('artist_name') or '').lower()}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
            all_tracks.append(t)

    # Create merged playlist
    await db.execute(
        "INSERT INTO playlists (name, description) VALUES (?, ?)",
        (target_name, f"Merged from {len(playlist_sources)} playlists"),
    )
    await db.commit()
    row = await db.fetchone("SELECT last_insert_rowid() as id")
    playlist_id = row["id"]

    # Add tracks (only those with local track_id)
    added = 0
    for i, t in enumerate(all_tracks):
        tid = t.get("track_id")
        if tid:
            await db.execute(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                (playlist_id, tid, i),
            )
            added += 1
        else:
            # Try to find in library
            found = await db.fetchone(
                "SELECT id FROM tracks WHERE title LIKE ? LIMIT 1",
                (f"%{t.get('title', '')}%",),
            )
            if found:
                await db.execute(
                    "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                    (playlist_id, found["id"], i),
                )
                added += 1
    await db.commit()

    return {
        "playlist_id": playlist_id,
        "playlist_name": target_name,
        "total_tracks": len(all_tracks),
        "added_to_playlist": added,
        "deduplicated": len(all_tracks) if deduplicate else 0,
        "sources": len(playlist_sources),
    }


def _event(type_str: str, data: dict):
    """Create an Event-like object."""
    from tune_server.event_bus import Event, EventType
    try:
        etype = EventType(type_str)
    except ValueError:
        etype = type_str
    return Event(type=etype, data=data, source="playlist_manager")

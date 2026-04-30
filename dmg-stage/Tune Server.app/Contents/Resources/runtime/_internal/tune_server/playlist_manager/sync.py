"""Playlist sync — pull/push/bidirectional sync between local and remote playlists."""

from __future__ import annotations

import json
from datetime import datetime

import structlog

from tune_server.playlist_manager.matcher import find_best_match, normalize

logger = structlog.get_logger()


async def sync_playlist(
    link: dict,
    db,
    streaming_service,
    search_func=None,
    match_threshold: float = 0.7,
) -> dict:
    """Sync a linked playlist.

    Args:
        link: playlist_links row dict
        db: Database instance
        streaming_service: StreamingService instance (for the remote side)
        search_func: async (query, limit) -> list[dict] for matching
        match_threshold: Minimum score for matching

    Returns:
        SyncResult dict
    """
    direction = link.get("sync_direction", "pull")
    local_playlist_id = link["local_playlist_id"]
    service_playlist_id = link["service_playlist_id"]

    added_to_local = 0
    removed_from_local = 0
    added_to_remote = 0
    removed_from_remote = 0
    conflicts = []

    # Load local tracks
    local_rows = await db.fetchall(
        """SELECT t.title, t.artist_name, t.source_id, t.id as track_id
           FROM playlist_tracks pt
           JOIN tracks t ON t.id = pt.track_id
           WHERE pt.playlist_id = ?
           ORDER BY pt.position""",
        (local_playlist_id,),
    )
    local_tracks = [dict(r) for r in local_rows]
    local_titles = {normalize(r["title"]) + "|" + normalize(r.get("artist_name") or "") for r in local_tracks}

    # Load remote tracks
    remote_tracks = []
    try:
        raw = await streaming_service.get_playlist_tracks(service_playlist_id)
        remote_tracks = [
            {
                "title": getattr(t, "title", "") if hasattr(t, "title") else t.get("title", ""),
                "artist_name": getattr(t, "artist_name", "") if hasattr(t, "artist_name") else t.get("artist_name", ""),
                "source_id": getattr(t, "source_id", "") if hasattr(t, "source_id") else t.get("source_id", ""),
                "duration_ms": getattr(t, "duration_ms", 0) if hasattr(t, "duration_ms") else t.get("duration_ms", 0),
            }
            for t in raw
        ]
    except Exception:
        logger.exception("sync_load_remote_error")
        return {"error": "Failed to load remote playlist"}

    remote_titles = {normalize(r["title"]) + "|" + normalize(r.get("artist_name") or "") for r in remote_tracks}

    # Pull: add remote tracks missing from local
    if direction in ("pull", "bidirectional"):
        for rt in remote_tracks:
            key = normalize(rt["title"]) + "|" + normalize(rt.get("artist_name") or "")
            if key not in local_titles:
                # Try to find in local library
                track_row = await db.fetchone(
                    """SELECT id FROM tracks
                       WHERE title LIKE ? AND (artist_name LIKE ? OR ? = '')
                       LIMIT 1""",
                    (f"%{rt['title']}%", f"%{rt.get('artist_name', '')}%", rt.get("artist_name", "")),
                )
                if track_row:
                    pos = len(local_tracks) + added_to_local
                    await db.execute(
                        "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                        (local_playlist_id, track_row["id"], pos),
                    )
                    added_to_local += 1
                else:
                    conflicts.append({
                        "track_title": rt["title"],
                        "artist": rt.get("artist_name", ""),
                        "issue": "Track from remote not found in local library",
                        "resolution": "skipped",
                    })

    # Push: add local tracks missing from remote
    if direction in ("push", "bidirectional") and streaming_service.supports_playlist_write:
        missing_on_remote = []
        for lt in local_tracks:
            key = normalize(lt["title"]) + "|" + normalize(lt.get("artist_name") or "")
            if key not in remote_titles and search_func:
                # Match local track on remote service
                result = await find_best_match(
                    source_title=lt["title"],
                    source_artist=lt.get("artist_name", ""),
                    search_func=search_func,
                    threshold=match_threshold,
                )
                if result.best_match and result.best_match.source_id:
                    missing_on_remote.append(result.best_match.source_id)
                else:
                    conflicts.append({
                        "track_title": lt["title"],
                        "artist": lt.get("artist_name", ""),
                        "issue": "Local track not found on remote service",
                        "resolution": "skipped",
                    })

        if missing_on_remote:
            added_to_remote = await streaming_service.add_tracks_to_playlist(
                service_playlist_id, missing_on_remote
            )

    await db.commit()

    # Update last_synced_at
    await db.execute(
        "UPDATE playlist_links SET last_synced_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), link["id"]),
    )
    await db.commit()

    return {
        "added_to_local": added_to_local,
        "removed_from_local": removed_from_local,
        "added_to_remote": added_to_remote,
        "removed_from_remote": removed_from_remote,
        "conflicts": conflicts,
    }

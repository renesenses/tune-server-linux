"""Playlist sync -- pull/push/bidirectional sync between local and remote playlists.

Supports snapshot-based delta detection for bidirectional sync:
- Tracks added/removed since last sync are detected by comparing current
  state against the previous snapshot stored in `sync_link_snapshots`.
- Conflicts (removed on one side, still present on other) default to
  following the removal (last-writer-wins), logged for transparency.
"""

from __future__ import annotations

import json
from datetime import datetime

import structlog

from tune_server.playlist_manager.matcher import find_best_match, normalize

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def _track_key(t: dict) -> str:
    """Canonical key for deduplication: normalized title|artist."""
    return normalize(t.get("title") or "") + "|" + normalize(t.get("artist_name") or "")


async def _save_snapshot(db, link_id: int, side: str, tracks: list[dict]) -> None:
    """Persist the current track list for a link side."""
    compact = [
        {"title": t.get("title", ""), "artist_name": t.get("artist_name", ""),
         "source_id": t.get("source_id", "")}
        for t in tracks
    ]
    await db.execute(
        """INSERT INTO sync_link_snapshots (playlist_link_id, side, tracks_json, created_at)
           VALUES (?, ?, ?, ?)""",
        (link_id, side, json.dumps(compact), datetime.utcnow().isoformat()),
    )
    await db.commit()


async def _load_last_snapshot(db, link_id: int, side: str) -> list[dict] | None:
    """Load the most recent snapshot for a link side. Returns None if none exists."""
    row = await db.fetchone(
        """SELECT tracks_json FROM sync_link_snapshots
           WHERE playlist_link_id = ? AND side = ?
           ORDER BY created_at DESC LIMIT 1""",
        (link_id, side),
    )
    if not row:
        return None
    try:
        return json.loads(row["tracks_json"])
    except (json.JSONDecodeError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

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
        SyncResult dict with counts, conflicts, and snapshot status
    """
    direction = link.get("sync_direction", "pull")
    link_id = link["id"]
    local_playlist_id = link["local_playlist_id"]
    service_playlist_id = link["service_playlist_id"]

    added_to_local = 0
    removed_from_local = 0
    added_to_remote = 0
    removed_from_remote = 0
    conflicts: list[dict] = []

    # ------------------------------------------------------------------
    # Load local tracks
    # ------------------------------------------------------------------
    local_rows = await db.fetchall(
        """SELECT t.title, t.artist_name, t.source_id, t.id as track_id
           FROM playlist_tracks pt
           JOIN tracks t ON t.id = pt.track_id
           WHERE pt.playlist_id = ?
           ORDER BY pt.position""",
        (local_playlist_id,),
    )
    local_tracks = [dict(r) for r in local_rows]
    local_keys = {_track_key(r) for r in local_tracks}

    # ------------------------------------------------------------------
    # Load remote tracks
    # ------------------------------------------------------------------
    remote_tracks: list[dict] = []
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

    remote_keys = {_track_key(r) for r in remote_tracks}

    # ------------------------------------------------------------------
    # Snapshot-based delta detection (bidirectional mode)
    # ------------------------------------------------------------------
    prev_local_snap = await _load_last_snapshot(db, link_id, "local")
    prev_remote_snap = await _load_last_snapshot(db, link_id, "remote")

    if direction == "bidirectional" and prev_local_snap is not None and prev_remote_snap is not None:
        # Previous state keys
        prev_local_keys = {_track_key(t) for t in prev_local_snap}
        prev_remote_keys = {_track_key(t) for t in prev_remote_snap}

        # Deltas
        added_on_local = local_keys - prev_local_keys
        removed_from_local_side = prev_local_keys - local_keys
        added_on_remote = remote_keys - prev_remote_keys
        removed_from_remote_side = prev_remote_keys - remote_keys

        # ------ Apply non-conflicting additions ------

        # Tracks added on remote -> add to local (if not already there)
        for key in added_on_remote:
            if key in local_keys:
                continue  # already present
            rt = next((t for t in remote_tracks if _track_key(t) == key), None)
            if not rt:
                continue
            track_row = await _find_local_track(db, rt)
            if track_row:
                pos = len(local_tracks) + added_to_local
                await db.execute(
                    "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                    (local_playlist_id, track_row["id"], pos),
                )
                added_to_local += 1
            else:
                conflicts.append({
                    "track_title": rt.get("title", ""),
                    "artist": rt.get("artist_name", ""),
                    "issue": "Track added on remote not found in local library",
                    "resolution": "skipped",
                })

        # Tracks added on local -> push to remote
        if streaming_service.supports_playlist_write and search_func:
            ids_to_push: list[str] = []
            for key in added_on_local:
                if key in remote_keys:
                    continue  # already present
                lt = next((t for t in local_tracks if _track_key(t) == key), None)
                if not lt:
                    continue
                result = await find_best_match(
                    source_title=lt.get("title", ""),
                    source_artist=lt.get("artist_name", ""),
                    search_func=search_func,
                    threshold=match_threshold,
                )
                if result.best_match and result.best_match.source_id:
                    ids_to_push.append(result.best_match.source_id)
                else:
                    conflicts.append({
                        "track_title": lt.get("title", ""),
                        "artist": lt.get("artist_name", ""),
                        "issue": "Local track not found on remote service",
                        "resolution": "skipped",
                    })

            if ids_to_push:
                added_to_remote = await streaming_service.add_tracks_to_playlist(
                    service_playlist_id, ids_to_push
                )

        # ------ Apply removals (last-writer-wins) ------

        # Removed on remote -> also remove from local
        for key in removed_from_remote_side:
            if key in added_on_local:
                # Conflict: removed on remote but added on local
                # Follow removal (last-writer-wins)
                lt = next((t for t in local_tracks if _track_key(t) == key), None)
                if lt:
                    conflicts.append({
                        "track_title": lt.get("title", ""),
                        "artist": lt.get("artist_name", ""),
                        "issue": "Removed on remote, still present on local",
                        "resolution": "removed_from_local",
                    })
            if key in local_keys and key not in added_on_local:
                # Track was in prev_remote, is now gone, and is still in local -> remove from local
                lt = next((t for t in local_tracks if _track_key(t) == key), None)
                if lt and lt.get("track_id"):
                    await db.execute(
                        "DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
                        (local_playlist_id, lt["track_id"]),
                    )
                    removed_from_local += 1

        # Removed on local -> also remove from remote
        if streaming_service.supports_playlist_write:
            remote_ids_to_remove: list[str] = []
            for key in removed_from_local_side:
                if key in added_on_remote:
                    rt = next((t for t in remote_tracks if _track_key(t) == key), None)
                    if rt:
                        conflicts.append({
                            "track_title": rt.get("title", ""),
                            "artist": rt.get("artist_name", ""),
                            "issue": "Removed on local, still present on remote",
                            "resolution": "removed_from_remote",
                        })
                if key in remote_keys and key not in added_on_remote:
                    rt = next((t for t in remote_tracks if _track_key(t) == key), None)
                    if rt and rt.get("source_id"):
                        remote_ids_to_remove.append(rt["source_id"])

            if remote_ids_to_remove and hasattr(streaming_service, "remove_tracks_from_playlist"):
                try:
                    removed_from_remote = await streaming_service.remove_tracks_from_playlist(
                        service_playlist_id, remote_ids_to_remove
                    )
                except Exception:
                    logger.exception("sync_remove_remote_error")
                    for sid in remote_ids_to_remove:
                        conflicts.append({
                            "track_title": "",
                            "artist": "",
                            "issue": f"Failed to remove track {sid} from remote",
                            "resolution": "skipped",
                        })

    else:
        # ------------------------------------------------------------------
        # Non-delta mode: simple pull/push (first sync or non-bidirectional)
        # ------------------------------------------------------------------

        # Pull: add remote tracks missing from local
        if direction in ("pull", "bidirectional"):
            for rt in remote_tracks:
                key = _track_key(rt)
                if key not in local_keys:
                    track_row = await _find_local_track(db, rt)
                    if track_row:
                        pos = len(local_tracks) + added_to_local
                        await db.execute(
                            "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                            (local_playlist_id, track_row["id"], pos),
                        )
                        added_to_local += 1
                    else:
                        conflicts.append({
                            "track_title": rt.get("title", ""),
                            "artist": rt.get("artist_name", ""),
                            "issue": "Track from remote not found in local library",
                            "resolution": "skipped",
                        })

        # Push: add local tracks missing from remote
        if direction in ("push", "bidirectional") and streaming_service.supports_playlist_write:
            missing_on_remote: list[str] = []
            for lt in local_tracks:
                key = _track_key(lt)
                if key not in remote_keys and search_func:
                    result = await find_best_match(
                        source_title=lt.get("title", ""),
                        source_artist=lt.get("artist_name", ""),
                        search_func=search_func,
                        threshold=match_threshold,
                    )
                    if result.best_match and result.best_match.source_id:
                        missing_on_remote.append(result.best_match.source_id)
                    else:
                        conflicts.append({
                            "track_title": lt.get("title", ""),
                            "artist": lt.get("artist_name", ""),
                            "issue": "Local track not found on remote service",
                            "resolution": "skipped",
                        })

            if missing_on_remote:
                added_to_remote = await streaming_service.add_tracks_to_playlist(
                    service_playlist_id, missing_on_remote
                )

    await db.commit()

    # ------------------------------------------------------------------
    # Save snapshots for next delta detection
    # ------------------------------------------------------------------
    snapshot_saved = False
    try:
        await _save_snapshot(db, link_id, "local", local_tracks)
        await _save_snapshot(db, link_id, "remote", remote_tracks)
        snapshot_saved = True
    except Exception:
        logger.exception("sync_save_snapshot_error", link_id=link_id)

    # Update last_synced_at
    await db.execute(
        "UPDATE playlist_links SET last_synced_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), link_id),
    )
    await db.commit()

    return {
        "added_to_local": added_to_local,
        "removed_from_local": removed_from_local,
        "added_to_remote": added_to_remote,
        "removed_from_remote": removed_from_remote,
        "conflicts": conflicts,
        "snapshot_saved": snapshot_saved,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _find_local_track(db, remote_track: dict):
    """Try to find a matching track in the local library."""
    title = remote_track.get("title", "")
    artist = remote_track.get("artist_name", "")
    return await db.fetchone(
        """SELECT id FROM tracks
           WHERE title LIKE ? AND (artist_name LIKE ? OR ? = '')
           LIMIT 1""",
        (f"%{title}%", f"%{artist}%", artist),
    )

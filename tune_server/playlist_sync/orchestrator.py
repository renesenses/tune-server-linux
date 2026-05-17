"""PlaylistOrchestrator — coordinates import/export/transfer across services.

Uses streaming connectors from ``deps.streaming_services`` and the
TrackMatcher for cross-service fuzzy matching.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from tune_server.event_bus import Event, EventType
from tune_server.playlist_sync.matcher import TrackInfo, TrackMatcher, TrackMatchResult

if TYPE_CHECKING:
    from tune_server.api.deps import AppDeps

logger = structlog.get_logger()

# Known streaming services that support playlists
PLAYLIST_SERVICES = ("tidal", "qobuz", "spotify", "deezer", "youtube")


@dataclass
class TransferProgress:
    """Tracks the progress of a transfer operation."""

    id: int
    source_service: str
    target_service: str
    playlist_name: str
    total_tracks: int = 0
    processed: int = 0
    matched: int = 0
    approximate: int = 0
    not_found: int = 0
    status: str = "pending"  # pending, in_progress, completed, failed
    error: str = ""
    started_at: float = 0.0
    completed_at: float = 0.0
    track_results: list[dict] = field(default_factory=list)


# In-memory transfer store (keyed by transfer ID)
_transfers: dict[int, TransferProgress] = {}
_next_transfer_id = 1


def _new_transfer_id() -> int:
    global _next_transfer_id
    tid = _next_transfer_id
    _next_transfer_id += 1
    return tid


class PlaylistOrchestrator:
    """Coordinates playlist import, export, and cross-service transfer."""

    def __init__(self, deps: AppDeps) -> None:
        self._deps = deps
        self._matcher = TrackMatcher(threshold=0.6)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_service(self, name: str):
        """Return an authenticated streaming service or raise."""
        svc = self._deps.streaming_services.get(name)
        if not svc:
            raise ServiceError(f"{name} not configured")
        if not svc.is_authenticated:
            raise ServiceError(f"{name} not authenticated")
        return svc

    def list_connected_services(self) -> list[dict]:
        """Return services that support playlists and are authenticated."""
        result: list[dict] = []
        for name in PLAYLIST_SERVICES:
            svc = self._deps.streaming_services.get(name)
            if not svc:
                continue
            result.append(
                {
                    "service": name,
                    "authenticated": svc.is_authenticated,
                    "supports_write": svc.supports_playlist_write,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Import: streaming service -> local playlist
    # ------------------------------------------------------------------

    async def import_playlist(
        self,
        source_service: str,
        source_playlist_id: str,
        name: str | None = None,
    ) -> dict:
        """Import a playlist from a streaming service into the local library.

        Fetches tracks from the source service, upserts them as streaming
        tracks in the local DB, and creates a local playlist referencing them.

        Returns dict with playlist_id, name, tracks_imported.
        """
        svc = self._get_service(source_service)

        # Fetch source tracks
        tracks = await svc.get_playlist_tracks(source_playlist_id)

        # Resolve name
        if not name:
            playlists = await svc.get_user_playlists()
            match = next(
                (p for p in playlists if p.source_id == source_playlist_id), None
            )
            name = match.name if match else f"Import from {source_service}"

        # Create local playlist
        playlist_id = await self._deps.playlist_repo.create(name)

        # Upsert streaming tracks
        track_ids: list[int] = []
        for t in tracks:
            if t.source and t.source_id:
                existing = await self._deps.track_repo.get_by_source(
                    t.source, t.source_id
                )
                if existing:
                    track_ids.append(existing.id)
                else:
                    from tune_server.models import Track as TrackModel

                    track_obj = TrackModel(
                        title=t.title,
                        artist_name=t.artist_name,
                        album_title=t.album_title,
                        duration_ms=t.duration_ms,
                        format=t.format,
                        sample_rate=t.sample_rate,
                        bit_depth=t.bit_depth,
                        channels=t.channels,
                        cover_path=t.cover_path,
                        source=t.source,
                        source_id=t.source_id,
                    )
                    tid = await self._deps.track_repo.create(track_obj)
                    track_ids.append(tid)

        if track_ids:
            await self._deps.playlist_repo.add_tracks(playlist_id, track_ids)

        # Emit event
        if self._deps.event_bus:
            self._deps.event_bus.emit_nowait(
                Event(
                    type=EventType.PLAYLIST_CREATED,
                    data={"playlist_id": playlist_id, "name": name},
                    source="playlist_sync",
                )
            )

        return {
            "playlist_id": playlist_id,
            "name": name,
            "tracks_imported": len(track_ids),
        }

    # ------------------------------------------------------------------
    # Export: local playlist -> streaming service
    # ------------------------------------------------------------------

    async def export_playlist(
        self,
        playlist_id: int,
        target_service: str,
        name: str | None = None,
    ) -> dict:
        """Export a local playlist to a streaming service.

        Matches each local track against the target service, creates a new
        playlist on the service, and adds matched tracks.

        Returns dict with service_playlist_id, name, matched, not_found.
        """
        svc = self._get_service(target_service)
        if not svc.supports_playlist_write:
            raise ServiceError(f"{target_service} does not support playlist creation")

        # Load local playlist
        playlist = await self._deps.playlist_repo.get(playlist_id)
        if not playlist:
            raise NotFoundError("Playlist not found")
        tracks = await self._deps.playlist_repo.get_tracks(playlist_id)

        playlist_name = name or playlist.name

        # Match tracks on target service
        matched_ids: list[str] = []
        match_results: list[dict] = []

        for t in tracks:
            source_info = TrackInfo(
                title=t.title,
                artist_name=t.artist_name or "",
                album_title=t.album_title or "",
                duration_ms=t.duration_ms or 0,
                source_id=t.source_id or str(t.id),
            )
            mr = await self._matcher.find_match(source_info, svc.search)
            entry = {
                "title": t.title,
                "artist_name": t.artist_name or "",
                "status": mr.status,
                "score": mr.best_match.score if mr.best_match else 0.0,
            }
            if mr.best_match and mr.status in ("matched", "approximate"):
                matched_ids.append(mr.best_match.source_id)
                entry["target_id"] = mr.best_match.source_id
                entry["target_title"] = mr.best_match.title
            match_results.append(entry)

        # Create playlist on service and add tracks
        service_playlist_id = await svc.create_playlist(playlist_name)
        if service_playlist_id and matched_ids:
            await svc.add_tracks_to_playlist(service_playlist_id, matched_ids)

        matched = sum(1 for r in match_results if r["status"] == "matched")
        approximate = sum(1 for r in match_results if r["status"] == "approximate")
        not_found = sum(1 for r in match_results if r["status"] == "not_found")

        return {
            "service_playlist_id": service_playlist_id,
            "name": playlist_name,
            "total_tracks": len(tracks),
            "matched": matched,
            "approximate": approximate,
            "not_found": not_found,
            "tracks": match_results,
        }

    # ------------------------------------------------------------------
    # Transfer: service A -> service B (via matching)
    # ------------------------------------------------------------------

    async def transfer_playlist(
        self,
        source_service: str,
        source_playlist_id: str,
        target_service: str,
        name: str | None = None,
    ) -> TransferProgress:
        """Transfer a playlist between two streaming services.

        Runs in the background. Returns a TransferProgress that can be
        polled via ``get_transfer_status()``.
        """
        source_svc = self._get_service(source_service)

        # Resolve playlist name
        if not name:
            playlists = await source_svc.get_user_playlists()
            match = next(
                (p for p in playlists if p.source_id == source_playlist_id), None
            )
            name = match.name if match else f"Transfer from {source_service}"

        # Create progress tracker
        transfer = TransferProgress(
            id=_new_transfer_id(),
            source_service=source_service,
            target_service=target_service,
            playlist_name=name,
            status="in_progress",
            started_at=time.time(),
        )
        _transfers[transfer.id] = transfer

        # Run transfer in background
        asyncio.create_task(
            self._run_transfer(transfer, source_service, source_playlist_id, target_service, name)
        )

        return transfer

    async def _run_transfer(
        self,
        transfer: TransferProgress,
        source_service: str,
        source_playlist_id: str,
        target_service: str,
        name: str,
    ) -> None:
        """Execute the transfer (background task)."""
        try:
            source_svc = self._get_service(source_service)
            target_svc = self._get_service(target_service)

            # Fetch source tracks
            source_tracks = await source_svc.get_playlist_tracks(source_playlist_id)
            transfer.total_tracks = len(source_tracks)

            # Emit start event
            if self._deps.event_bus:
                self._deps.event_bus.emit_nowait(
                    Event(
                        type=EventType.PLAYLIST_MANAGER_TRANSFER_STARTED,
                        data={
                            "transfer_id": transfer.id,
                            "total": transfer.total_tracks,
                            "source": source_service,
                            "target": target_service,
                            "playlist_name": name,
                        },
                        source="playlist_sync",
                    )
                )

            # Match each track on the target service
            matched_ids: list[str] = []
            sem = asyncio.Semaphore(5)  # rate-limit

            async def _match_one(t) -> dict:
                async with sem:
                    source_info = TrackInfo(
                        title=t.title,
                        artist_name=t.artist_name or "",
                        album_title=t.album_title or "",
                        duration_ms=t.duration_ms or 0,
                        source_id=t.source_id or "",
                    )
                    mr = await self._matcher.find_match(source_info, target_svc.search)

                    entry = {
                        "title": t.title,
                        "artist_name": t.artist_name or "",
                        "status": mr.status,
                        "score": mr.best_match.score if mr.best_match else 0.0,
                    }
                    if mr.best_match and mr.status in ("matched", "approximate"):
                        matched_ids.append(mr.best_match.source_id)
                        entry["target_id"] = mr.best_match.source_id
                        entry["target_title"] = mr.best_match.title

                    transfer.processed += 1
                    if mr.status == "matched":
                        transfer.matched += 1
                    elif mr.status == "approximate":
                        transfer.approximate += 1
                    else:
                        transfer.not_found += 1

                    # Progress events every 5 tracks
                    if self._deps.event_bus and transfer.processed % 5 == 0:
                        self._deps.event_bus.emit_nowait(
                            Event(
                                type=EventType.PLAYLIST_MANAGER_TRANSFER_PROGRESS,
                                data={
                                    "transfer_id": transfer.id,
                                    "processed": transfer.processed,
                                    "total": transfer.total_tracks,
                                    "matched": transfer.matched,
                                    "approximate": transfer.approximate,
                                    "not_found": transfer.not_found,
                                },
                                source="playlist_sync",
                            )
                        )

                    return entry

            # Sequential matching (with semaphore for rate-limiting)
            for t in source_tracks:
                result = await _match_one(t)
                transfer.track_results.append(result)

            # Create playlist on target service if it supports writes
            service_playlist_id = None
            if target_svc.supports_playlist_write and matched_ids:
                service_playlist_id = await target_svc.create_playlist(name)
                if service_playlist_id:
                    await target_svc.add_tracks_to_playlist(
                        service_playlist_id, matched_ids
                    )

            # Also create a local playlist as a reference copy
            local_playlist_id = await self._deps.playlist_repo.create(name)

            # Upsert matched tracks from source into local DB
            local_track_ids: list[int] = []
            for t in source_tracks:
                if t.source and t.source_id:
                    existing = await self._deps.track_repo.get_by_source(
                        t.source, t.source_id
                    )
                    if existing:
                        local_track_ids.append(existing.id)
                    else:
                        from tune_server.models import Track as TrackModel

                        track_obj = TrackModel(
                            title=t.title,
                            artist_name=t.artist_name,
                            album_title=t.album_title,
                            duration_ms=t.duration_ms,
                            source=t.source,
                            source_id=t.source_id,
                        )
                        tid = await self._deps.track_repo.create(track_obj)
                        local_track_ids.append(tid)

            if local_track_ids:
                await self._deps.playlist_repo.add_tracks(
                    local_playlist_id, local_track_ids
                )

            transfer.status = "completed"
            transfer.completed_at = time.time()

            # Record in transfer_history if DB supports it
            if self._deps.db:
                try:
                    await self._deps.db.execute(
                        """INSERT INTO transfer_history
                           (operation, source_service, source_playlist_id,
                            source_playlist_name, target_service,
                            target_playlist_name, total_tracks, matched,
                            approximate, not_found, status, started_at,
                            completed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)""",
                        (
                            "sync_transfer",
                            source_service,
                            source_playlist_id,
                            name,
                            target_service,
                            name,
                            transfer.total_tracks,
                            transfer.matched,
                            transfer.approximate,
                            transfer.not_found,
                            transfer.started_at,
                            transfer.completed_at,
                        ),
                    )
                    await self._deps.db.commit()
                except Exception:
                    logger.debug("transfer_history_insert_error")

            # Emit completion event
            if self._deps.event_bus:
                self._deps.event_bus.emit_nowait(
                    Event(
                        type=EventType.PLAYLIST_MANAGER_TRANSFER_COMPLETED,
                        data={
                            "transfer_id": transfer.id,
                            "matched": transfer.matched,
                            "approximate": transfer.approximate,
                            "not_found": transfer.not_found,
                        },
                        source="playlist_sync",
                    )
                )

        except Exception as exc:
            logger.exception(
                "playlist_sync_transfer_error",
                transfer_id=transfer.id,
            )
            transfer.status = "failed"
            transfer.error = str(exc)
            transfer.completed_at = time.time()

    @staticmethod
    def get_transfer_status(transfer_id: int) -> TransferProgress | None:
        """Return the current status of a transfer operation."""
        return _transfers.get(transfer_id)


class ServiceError(Exception):
    """Raised when a streaming service is unavailable or misconfigured."""


class NotFoundError(Exception):
    """Raised when a requested resource does not exist."""

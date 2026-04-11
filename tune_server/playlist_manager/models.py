"""Pydantic models for the Playlist Manager."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------

class TransferRequest(BaseModel):
    source_service: str
    source_playlist_id: str
    target_service: str = "local"
    target_name: Optional[str] = None
    create_on_target: bool = False
    match_threshold: float = 0.6
    include_approximate: bool = True
    dry_run: bool = False


class TrackMatchResult(BaseModel):
    title: str
    artist_name: str
    album_title: str = ""
    status: str  # "matched", "approximate", "not_found"
    match_method: str = ""
    confidence: str = ""
    score: float = 0.0
    source_id: str = ""
    target_id: str = ""
    alternatives: list[dict] = []


class TransferResponse(BaseModel):
    transfer_id: Optional[int] = None
    source_name: str
    target_service: str
    target_playlist_id: Optional[str] = None
    local_playlist_id: Optional[int] = None
    total_tracks: int = 0
    matched: int = 0
    approximate: int = 0
    not_found: int = 0
    tracks: list[TrackMatchResult] = []


# ---------------------------------------------------------------------------
# Batch Transfer
# ---------------------------------------------------------------------------

class BatchTransferRequest(BaseModel):
    source_service: str
    target_service: str = "local"
    playlist_ids: Optional[list[str]] = None  # None = ALL
    match_threshold: float = 0.6


class BatchTransferResponse(BaseModel):
    batch_id: str
    total_playlists: int
    status: str = "in_progress"


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

class BackupRequest(BaseModel):
    services: Optional[list[str]] = None  # None = all
    include_tracks: bool = True


class BackupResponse(BaseModel):
    backup_id: int
    playlists_backed_up: int
    total_tracks_snapshot: int
    services: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Sync Links
# ---------------------------------------------------------------------------

class CreateLinkRequest(BaseModel):
    local_playlist_id: int
    service: str
    service_playlist_id: str
    sync_direction: str = "pull"  # "pull", "push", "bidirectional"
    sync_interval_minutes: int = 0  # 0 = manual


class PlaylistLink(BaseModel):
    id: int
    local_playlist_id: int
    service: str
    service_playlist_id: str
    service_playlist_name: Optional[str] = None
    sync_direction: str = "pull"
    last_synced_at: Optional[str] = None
    created_at: Optional[str] = None


class SyncResult(BaseModel):
    added_to_local: int = 0
    removed_from_local: int = 0
    added_to_remote: int = 0
    removed_from_remote: int = 0
    conflicts: list[dict] = []


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

class MergeRequest(BaseModel):
    playlists: list[dict]  # [{service, playlist_id}]
    target_name: str
    deduplicate: bool = True
    target_service: str = "local"


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------

class ExportRequest(BaseModel):
    service: str
    playlist_id: str
    format: str = "csv"  # "csv", "xspf", "text", "json"


# ---------------------------------------------------------------------------
# Conflict Resolution
# ---------------------------------------------------------------------------

class Resolution(BaseModel):
    source_track_index: int
    action: str  # "use_alternative", "skip", "manual_match"
    alternative_index: Optional[int] = None
    target_source_id: Optional[str] = None


class ResolveRequest(BaseModel):
    transfer_id: int
    resolutions: list[Resolution]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

class TransferHistoryEntry(BaseModel):
    id: int
    operation: str
    source_service: str
    source_playlist_name: Optional[str] = None
    target_service: Optional[str] = None
    target_playlist_name: Optional[str] = None
    total_tracks: int = 0
    matched: int = 0
    approximate: int = 0
    not_found: int = 0
    status: str = "completed"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

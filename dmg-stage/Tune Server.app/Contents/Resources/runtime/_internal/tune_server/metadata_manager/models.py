"""Pydantic models for the Metadata Manager."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class TrackMetadataUpdate(BaseModel):
    title: Optional[str] = None
    artist_name: Optional[str] = None
    album_title: Optional[str] = None
    album_id: Optional[int] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    genre: Optional[str] = None
    composer: Optional[str] = None
    year: Optional[int] = None
    lyrics: Optional[str] = None
    comment: Optional[str] = None
    isrc: Optional[str] = None
    musicbrainz_recording_id: Optional[str] = None
    bpm: Optional[int] = None
    label: Optional[str] = None
    custom_tags: Optional[dict] = None


class AlbumMetadataUpdate(BaseModel):
    title: Optional[str] = None
    artist_name: Optional[str] = None
    year: Optional[int] = None
    genre: Optional[str] = None
    musicbrainz_release_id: Optional[str] = None
    label: Optional[str] = None
    catalog_number: Optional[str] = None
    barcode: Optional[str] = None


class ArtistMetadataUpdate(BaseModel):
    name: Optional[str] = None
    sort_name: Optional[str] = None
    musicbrainz_id: Optional[str] = None
    bio: Optional[str] = None


class BatchTrackEditRequest(BaseModel):
    track_ids: list[int]
    updates: TrackMetadataUpdate


class RenameArtistRequest(BaseModel):
    old_name: str
    new_name: str
    update_files: bool = False


class BatchWriteTagsRequest(BaseModel):
    track_ids: list[int]


class MetadataSuggestion(BaseModel):
    id: int
    track_id: Optional[int] = None
    album_id: Optional[int] = None
    field: str
    current_value: Optional[str] = None
    suggested_value: str
    source: str
    confidence: float = 0.0
    status: str = "pending"

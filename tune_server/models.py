from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Source(str, Enum):
    LOCAL = "local"
    TIDAL = "tidal"
    QOBUZ = "qobuz"
    YOUTUBE = "youtube"
    AMAZON = "amazon"


class AudioFormat(str, Enum):
    FLAC = "flac"
    WAV = "wav"
    MP3 = "mp3"
    AAC = "aac"
    ALAC = "alac"
    OGG = "ogg"
    OPUS = "opus"
    DSD = "dsd"
    AIFF = "aiff"
    WMA = "wma"


class PlaybackState(str, Enum):
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"
    BUFFERING = "buffering"


class RepeatMode(str, Enum):
    OFF = "off"
    ONE = "one"
    ALL = "all"


class OutputType(str, Enum):
    LOCAL = "local"
    DLNA = "dlna"
    AIRPLAY = "airplay"


# --- Domain models ---


class Artist(BaseModel):
    id: Optional[int] = None
    name: str
    sort_name: Optional[str] = None
    musicbrainz_id: Optional[str] = None
    discogs_id: Optional[str] = None
    bio: Optional[str] = None
    image_path: Optional[str] = None


class Album(BaseModel):
    id: Optional[int] = None
    title: str
    artist_id: Optional[int] = None
    artist_name: Optional[str] = None
    year: Optional[int] = None
    genre: Optional[str] = None
    disc_count: int = 1
    track_count: int = 0
    cover_path: Optional[str] = None
    source: Source = Source.LOCAL
    source_id: Optional[str] = None


class Track(BaseModel):
    id: Optional[int] = None
    title: str
    album_id: Optional[int] = None
    album_title: Optional[str] = None
    artist_id: Optional[int] = None
    artist_name: Optional[str] = None
    disc_number: int = 1
    track_number: int = 0
    duration_ms: int = 0
    file_path: Optional[str] = None
    format: Optional[AudioFormat] = None
    sample_rate: Optional[int] = None
    bit_depth: Optional[int] = None
    channels: int = 2
    source: Source = Source.LOCAL
    source_id: Optional[str] = None


class Playlist(BaseModel):
    id: Optional[int] = None
    name: str
    description: Optional[str] = None
    track_count: int = 0


class PlaylistTrack(BaseModel):
    playlist_id: int
    track_id: int
    position: int


class QueueItem(BaseModel):
    id: Optional[int] = None
    zone_id: int
    track_id: int
    position: int
    track: Optional[Track] = None


class Zone(BaseModel):
    id: Optional[int] = None
    name: str
    output_type: OutputType = OutputType.LOCAL
    output_device_id: Optional[str] = None
    volume: float = Field(default=0.5, ge=0.0, le=1.0)
    group_id: Optional[str] = None
    state: PlaybackState = PlaybackState.STOPPED
    current_track: Optional[Track] = None
    position_ms: int = 0
    queue_length: int = 0


class DiscoveredDevice(BaseModel):
    id: str
    name: str
    type: OutputType
    host: str
    port: int
    available: bool = True
    capabilities: dict = Field(default_factory=dict)


class AudioStreamInfo(BaseModel):
    format: AudioFormat
    sample_rate: int
    bit_depth: int
    channels: int
    duration_ms: int = 0
    file_size: Optional[int] = None


# --- API request/response models ---


class PlayRequest(BaseModel):
    track_id: Optional[int] = None
    track_ids: Optional[list[int]] = None
    album_id: Optional[int] = None
    playlist_id: Optional[int] = None
    source: Optional[Source] = None
    source_id: Optional[str] = None


class QueueAddRequest(BaseModel):
    track_id: Optional[int] = None
    track_ids: Optional[list[int]] = None
    album_id: Optional[int] = None
    source: Optional[Source] = None
    source_id: Optional[str] = None
    position: Optional[int] = None  # None = append to end


class ZoneCreateRequest(BaseModel):
    name: str
    output_type: OutputType = OutputType.LOCAL
    output_device_id: Optional[str] = None


class ZoneGroupRequest(BaseModel):
    zone_ids: list[int]
    leader_id: int


class VolumeRequest(BaseModel):
    volume: float = Field(ge=0.0, le=1.0)


class QueueMoveRequest(BaseModel):
    from_position: int = Field(ge=0)
    to_position: int = Field(ge=0)


class QueueJumpRequest(BaseModel):
    position: int = Field(ge=0)


class SeekRequest(BaseModel):
    position_ms: int = Field(ge=0)


class SearchResult(BaseModel):
    tracks: list[Track] = Field(default_factory=list)
    albums: list[Album] = Field(default_factory=list)
    artists: list[Artist] = Field(default_factory=list)


class PlaylistCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None


class PlaylistUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class PlaylistAddTracksRequest(BaseModel):
    track_ids: list[int]
    position: Optional[int] = None  # None = append


class PlaylistReorderRequest(BaseModel):
    track_ids: list[int]  # Full ordered list


class TrackUpdateRequest(BaseModel):
    title: Optional[str] = None
    album_id: Optional[int] = None
    artist_id: Optional[int] = None
    disc_number: Optional[int] = None
    track_number: Optional[int] = None


class AlbumUpdateRequest(BaseModel):
    title: Optional[str] = None
    artist_id: Optional[int] = None
    year: Optional[int] = None
    genre: Optional[str] = None


class ArtistUpdateRequest(BaseModel):
    name: Optional[str] = None
    sort_name: Optional[str] = None
    bio: Optional[str] = None


# --- Typed API response models ---


class QueueStateResponse(BaseModel):
    tracks: list[Track] = Field(default_factory=list)
    position: int = 0
    length: int = 0


class ShuffleResponse(BaseModel):
    shuffle: bool


class RepeatResponse(BaseModel):
    repeat: RepeatMode


class QueueLengthResponse(BaseModel):
    queue_length: int


class LibraryStatsResponse(BaseModel):
    tracks: int = 0
    albums: int = 0
    artists: int = 0


class SystemHealthResponse(BaseModel):
    status: str
    components: dict[str, bool]


class SystemConfigResponse(BaseModel):
    music_dirs: list[str]
    api_port: int
    stream_port: int
    tidal_enabled: bool
    qobuz_enabled: bool
    youtube_enabled: bool = False
    amazon_music_enabled: bool = False
    discovery_enabled: bool


class ScanStatusResponse(BaseModel):
    scanning: bool


class StreamingServiceStatus(BaseModel):
    enabled: bool
    authenticated: bool


class StreamingAuthRequest(BaseModel):
    username: str | None = None
    password: str | None = None
    oauth_json: str | None = None


class StreamingAuthResponse(BaseModel):
    authenticated: bool


class ZoneGroupResponse(BaseModel):
    group_id: str
    leader_id: int
    zone_ids: list[int]


class FederatedSearchResult(BaseModel):
    """Combined search results from library + streaming services."""
    local: SearchResult = Field(default_factory=SearchResult)
    services: dict[str, SearchResult] = Field(default_factory=dict)


class SystemStatsResponse(BaseModel):
    tracks: int = 0
    albums: int = 0
    artists: int = 0
    zones: int = 0
    devices: int = 0


class WsSubscribeMessage(BaseModel):
    action: str  # "subscribe" or "unsubscribe"
    patterns: list[str]  # e.g. ["playback.*", "zone.1.*"]

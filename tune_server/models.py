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
    SPOTIFY = "spotify"
    DEEZER = "deezer"
    RADIO = "radio"


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
    cover_path: Optional[str] = None
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
    id: int | None = None
    name: str
    output_type: OutputType = OutputType.LOCAL
    output_device_id: str | None = None
    volume: float = Field(default=0.5, ge=0.0, le=1.0)
    group_id: str | None = None
    sync_delay_ms: int = 0
    state: PlaybackState = PlaybackState.STOPPED
    current_track: Track | None = None
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


class LocalAudioDevice(BaseModel):
    id: str
    name: str
    channels: int
    sample_rate: int


class AudioStreamInfo(BaseModel):
    format: AudioFormat
    sample_rate: int
    bit_depth: int
    channels: int
    duration_ms: int = 0
    file_size: Optional[int] = None


# --- Radio station models ---


class RadioStation(BaseModel):
    id: Optional[int] = None
    name: str
    stream_url: str
    logo_url: Optional[str] = None
    genre: Optional[str] = None
    tags: Optional[str] = None
    codec: Optional[str] = None
    country: Optional[str] = None
    homepage_url: Optional[str] = None
    favorite: bool = False


class RadioStationCreate(BaseModel):
    name: str
    stream_url: str
    logo_url: Optional[str] = None
    genre: Optional[str] = None
    tags: Optional[str] = None
    codec: Optional[str] = None
    country: Optional[str] = None
    homepage_url: Optional[str] = None
    favorite: bool = False


class RadioStationUpdate(BaseModel):
    name: Optional[str] = None
    stream_url: Optional[str] = None
    logo_url: Optional[str] = None
    genre: Optional[str] = None
    tags: Optional[str] = None
    codec: Optional[str] = None
    country: Optional[str] = None
    homepage_url: Optional[str] = None
    favorite: Optional[bool] = None


class RadioImportResult(BaseModel):
    imported: int
    skipped: int
    errors: list[str]


# --- API request/response models ---


class StreamingPlaylist(BaseModel):
    source_id: str
    name: str
    description: Optional[str] = None
    track_count: int = 0
    duration_ms: int = 0
    cover_path: Optional[str] = None
    source: Source


class PlayRequest(BaseModel):
    track_id: Optional[int] = None
    track_ids: Optional[list[int]] = None
    album_id: Optional[int] = None
    playlist_id: Optional[int] = None
    source: Optional[Source] = None
    source_id: Optional[str] = None
    streaming_playlist_id: Optional[str] = None


class QueueAddRequest(BaseModel):
    track_id: Optional[int] = None
    track_ids: Optional[list[int]] = None
    album_id: Optional[int] = None
    source: Optional[Source] = None
    source_id: Optional[str] = None
    position: Optional[int] = None  # None = append to end


class ZoneCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    output_type: OutputType = OutputType.LOCAL
    output_device_id: str | None = None
    sync_delay_ms: int = Field(default=0, ge=-10000, le=10000)


class ZoneUpdateRequest(BaseModel):
    name: str | None = None
    sync_delay_ms: int | None = Field(default=None, ge=-10000, le=10000)


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


class StreamingTrackInfo(BaseModel):
    source: Source
    source_id: str
    title: str
    artist_name: Optional[str] = None
    album_title: Optional[str] = None
    duration_ms: int = 0
    format: Optional[AudioFormat] = None
    sample_rate: Optional[int] = None
    bit_depth: Optional[int] = None
    channels: int = 2
    cover_path: Optional[str] = None


class PlaylistAddTracksRequest(BaseModel):
    track_ids: list[int] = Field(default_factory=list)
    streaming_tracks: list[StreamingTrackInfo] = Field(default_factory=list)
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
    spotify_enabled: bool = False
    deezer_enabled: bool = False
    discovery_enabled: bool
    sync_poll_playing_interval: float = 1.0
    sync_poll_idle_interval: float = 10.0
    sync_drift_threshold_ms: int = 500
    sync_correction_cooldown_s: float = 15.0
    sync_dlna_default_buffer_s: float = 3.0


class MusicDirRequest(BaseModel):
    path: str


class MusicDirsResponse(BaseModel):
    music_dirs: list[str]


class ScanStatusResponse(BaseModel):
    scanning: bool


class BackupInfo(BaseModel):
    filename: str
    size: int
    created_at: str


class FeaturedSection(BaseModel):
    id: str
    name: str


class StreamingServiceStatus(BaseModel):
    enabled: bool
    authenticated: bool


class StreamingAuthRequest(BaseModel):
    username: str | None = None
    password: str | None = None
    oauth_json: str | None = None


class StreamingAuthResponse(BaseModel):
    authenticated: bool
    verification_url: Optional[str] = None
    user_code: Optional[str] = None


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


class CompletenessStats(BaseModel):
    total_albums: int = 0
    albums_without_cover: int = 0
    albums_without_genre: int = 0
    albums_without_year: int = 0
    total_artists: int = 0
    artists_without_image: int = 0
    total_tracks: int = 0
    tracks_without_artist: int = 0


# --- Browse by directory models ---


class BrowseDirectory(BaseModel):
    name: str
    path: str
    track_count: int = 0


class BrowseResult(BaseModel):
    path: str
    parent: Optional[str] = None
    music_root: str
    directories: list[BrowseDirectory] = Field(default_factory=list)
    tracks: list[Track] = Field(default_factory=list)


class BrowseRootEntry(BaseModel):
    name: str
    path: str
    track_count: int = 0


class BrowseRootsResponse(BaseModel):
    roots: list[BrowseRootEntry] = Field(default_factory=list)


# --- Network discovery & browsing models ---


class ShareProtocol(str, Enum):
    SMB = "smb"
    NFS = "nfs"


class NetworkShare(BaseModel):
    id: str  # "smb://host/share" or "nfs://host/export"
    name: str  # mDNS name or hostname
    host: str
    port: int = 0
    protocol: ShareProtocol
    shares: list[str] = Field(default_factory=list)
    available: bool = True


class MountRequest(BaseModel):
    host: str
    share_name: str  # e.g. "Music" (SMB) or "/export/music" (NFS)
    protocol: ShareProtocol
    username: Optional[str] = None
    password: Optional[str] = None
    auto_mount: bool = True


class MountInfo(BaseModel):
    id: int
    host: str
    share_name: str
    protocol: ShareProtocol
    mount_path: str
    status: str  # "mounted", "unmounted", "error"
    auto_mount: bool = True
    error: Optional[str] = None


class DiscoveredMediaServer(BaseModel):
    id: str
    name: str
    host: str
    port: int
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    available: bool = True


class MediaServerContainer(BaseModel):
    id: str
    parent_id: str
    title: str
    child_count: int = 0


class MediaServerItem(BaseModel):
    id: str
    parent_id: str
    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    res_url: Optional[str] = None
    duration_ms: int = 0
    album_art_uri: Optional[str] = None


class MediaServerBrowseResult(BaseModel):
    object_id: str
    containers: list[MediaServerContainer] = Field(default_factory=list)
    items: list[MediaServerItem] = Field(default_factory=list)
    total_matches: int = 0
    number_returned: int = 0

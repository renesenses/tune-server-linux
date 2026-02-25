from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TUNE_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # Library
    music_dirs: list[str] = Field(default_factory=lambda: [str(Path.home() / "Music")])
    scan_on_startup: bool = True
    watch_filesystem: bool = True
    watcher_debounce_seconds: float = 2.0

    # Database
    db_path: str = "tune_server.db"

    # Security
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    api_key: Optional[str] = None  # None = no auth required (backward-compatible)

    # Web UI (built SPA served as static files, empty = disabled)
    web_dir: Optional[str] = None

    # Server
    api_host: str = "0.0.0.0"
    api_port: int = 8888
    stream_host: str = "0.0.0.0"
    stream_port: int = 8080

    # WebSocket
    ws_heartbeat_interval: int = 30  # seconds, 0 to disable

    # HTTP Streaming
    http_session_timeout: int = 300  # seconds (5 min)

    # Playback
    stream_url_resolve_timeout: int = 15  # seconds
    pipeline_start_timeout: int = 30  # seconds

    # Audio
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    default_output_format: str = "flac"  # flac, wav, mp3, alac
    max_sample_rate: int = 192000
    max_bit_depth: int = 24

    # Artwork
    artwork_cache_dir: str = "artwork_cache"
    artwork_max_size: int = 1200  # max dimension in pixels

    # Streaming services
    tidal_enabled: bool = False
    tidal_quality: str = "HI_RES_LOSSLESS"  # LOW, HIGH, LOSSLESS, HI_RES_LOSSLESS

    qobuz_enabled: bool = False
    qobuz_app_id: Optional[str] = None
    qobuz_app_secret: Optional[str] = None

    # YouTube Music
    youtube_enabled: bool = False
    youtube_oauth_json: Optional[str] = None  # Path to oauth.json
    youtube_url_cache_ttl: int = 3600  # seconds (YouTube URLs expire ~6h)

    # Amazon Music
    amazon_music_enabled: bool = False
    amazon_music_region: str = "us"
    amazon_music_quality: str = "HD"  # SD, HD, ULTRA_HD

    # Discovery
    discovery_enabled: bool = True
    ssdp_enabled: bool = True
    mdns_enabled: bool = True

    # Logging
    log_level: str = "INFO"
    log_format: str = "console"  # console or json


settings = Settings()

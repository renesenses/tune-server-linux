from __future__ import annotations

import re
from pathlib import Path

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
    api_key: str | None = None  # None = no auth required (backward-compatible)

    # Web UI (built SPA served as static files, empty = disabled)
    web_dir: str | None = None

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

    # Multi-room sync
    sync_poll_playing_interval: float = 1.0       # seconds, when groups are playing
    sync_poll_idle_interval: float = 10.0          # seconds, when no active groups
    sync_drift_threshold_ms: int = 500             # ms, correct if drift exceeds this
    sync_correction_cooldown_s: float = 15.0       # seconds, min between corrections per follower
    sync_dlna_default_buffer_s: float = 3.0        # seconds, default DLNA buffer delay

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
    qobuz_app_id: str | None = None
    qobuz_app_secret: str | None = None

    # YouTube Music
    youtube_enabled: bool = False
    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_oauth_json: str | None = None  # Legacy: path to oauth.json file
    youtube_url_cache_ttl: int = 3600  # seconds (YouTube URLs expire ~6h)

    # Amazon Music
    amazon_music_enabled: bool = False
    amazon_music_region: str = "us"
    amazon_music_quality: str = "HD"  # SD, HD, ULTRA_HD

    # Spotify
    spotify_enabled: bool = False
    spotify_client_id: str | None = None
    spotify_redirect_uri: str = "http://localhost:8888/api/v1/streaming/spotify/callback"

    # Deezer
    deezer_enabled: bool = False
    deezer_app_id: str | None = None
    deezer_app_secret: str | None = None
    deezer_redirect_uri: str = "http://localhost:8888/api/v1/streaming/deezer/callback"

    # Discovery
    discovery_enabled: bool = True
    ssdp_enabled: bool = True
    mdns_enabled: bool = True

    # Network shares & media servers
    network_shares_enabled: bool = False
    network_media_servers_enabled: bool = False
    smb_mount_dir: str = "~/.tune/mounts"

    # Logging
    log_level: str = "INFO"
    log_format: str = "console"  # console or json


def _auto_detect_web_dir() -> str | None:
    """Look for a web/ folder next to the executable or working directory."""
    import sys
    candidates = [
        Path(sys.executable).parent / "web",  # PyInstaller bundle
        Path.cwd() / "web",
        Path(__file__).resolve().parent.parent / "web",
    ]
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return str(candidate)
    return None


def _auto_detect_ffmpeg(name: str) -> str:
    """Find ffmpeg/ffprobe next to executable, in cwd, or fall back to PATH."""
    import shutil
    import sys
    ext = ".exe" if sys.platform == "win32" else ""
    candidates = [
        Path(sys.executable).parent / f"{name}{ext}",
        Path.cwd() / f"{name}{ext}",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name) or name


_raw = Settings()
if _raw.web_dir is None:
    _raw.web_dir = _auto_detect_web_dir()
if _raw.ffmpeg_path == "ffmpeg":
    _raw.ffmpeg_path = _auto_detect_ffmpeg("ffmpeg")
if _raw.ffprobe_path == "ffprobe":
    _raw.ffprobe_path = _auto_detect_ffmpeg("ffprobe")
settings = _raw


def persist_env_var(key: str, value: str, env_file: str = ".env") -> None:
    """Update or add a key=value in the .env file."""
    path = Path(env_file)
    lines: list[str] = []
    found = False

    if path.exists():
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if re.match(rf"^{re.escape(key)}\s*=", line):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)

    if not found:
        lines.append(f"{key}={value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

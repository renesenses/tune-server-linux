from __future__ import annotations

import re
import sys
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _detect_base_dir() -> Path:
    """Return the directory containing the binary (PyInstaller) or the CWD."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _env_file_candidates() -> list[str]:
    """Build ordered list of .env paths: exe dir, %APPDATA%/TuneServer, CWD."""
    candidates = [str(_detect_base_dir() / ".env")]
    if sys.platform == "win32":
        import os
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        candidates.append(str(Path(appdata) / "TuneServer" / ".env"))
    candidates.append(".env")
    return candidates


def _detect_web_dir() -> str | None:
    """Auto-detect web/ directory next to the binary or in _internal/."""
    base = _detect_base_dir()
    for candidate in [base / "web", base / "_internal" / "web"]:
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return str(candidate)
    return None


def _detect_bin(name: str) -> str:
    """Auto-detect a bundled binary next to the executable, fallback to bare name (PATH)."""
    base = _detect_base_dir()
    for suffix in ("", ".exe"):
        candidate = base / (name + suffix)
        if candidate.is_file():
            return str(candidate)
    return name


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TUNE_",
        env_file=_env_file_candidates(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Rust native acceleration (tune_native via PyO3)
    # "auto" = use Rust if available, "rust" = require Rust, "python" = force Python
    scanner_engine: str = "auto"
    discovery_engine: str = "auto"
    metadata_engine: str = "auto"
    # HTTP streamer engine: "auto" = Rust sidecar if available, "rust" = require, "python" = force aiohttp
    streamer_engine: str = "auto"

    # Library
    music_dirs: list[str] = Field(default_factory=lambda: [str(Path.home() / "Music")])
    scan_on_startup: bool = True
    scan_schedule: str | None = None  # "HH:MM" for daily scheduled scan, e.g. "03:00"
    quality_split: bool = True  # Split albums by quality tier (44.1 vs 96 kHz)
    watch_filesystem: bool = True
    watcher_debounce_seconds: float = 2.0

    # Database
    db_path: str = "tune_server.db"
    db_engine: str = "sqlite"  # "sqlite" or "postgres"
    db_url: str | None = None  # PostgreSQL URL, e.g. "postgresql://user:pass@localhost/tune"
    db_pool_min: int = 2  # PostgreSQL connection pool min size
    db_pool_max: int = 10  # PostgreSQL connection pool max size

    # Security
    # CORS: allow all origins — this is a LAN-only server, protected by API key
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    api_key: str | None = None  # None = no auth required (backward-compatible)

    # Web UI (auto-detected next to binary, set to empty string to disable)
    web_dir: str | None = Field(default_factory=_detect_web_dir)

    # Server
    api_host: str = "0.0.0.0"
    api_port: int = 8888
    stream_host: str = "0.0.0.0"
    stream_port: int = 8080
    advertise_ip: str | None = None
    default_zone_id: int | None = None

    # Qobuz outbound proxy. Akamai blacklists most Free / OVH / datacenter
    # ASNs at the CDN edge, so direct calls to www.qobuz.com return a hard
    # 403 even with a valid app_id. Set this to a SOCKS5 / HTTP proxy that
    # exits through a residential ASN (NordVPN, Mullvad, your phone's 4G,
    # etc.) and Qobuz traffic only will be tunnelled through it. Empty
    # = direct (default, works fine on residential ISPs).
    qobuz_proxy_url: str | None = None

    # WebSocket
    ws_heartbeat_interval: int = 30  # seconds, 0 to disable

    # HTTP Streaming
    http_session_timeout: int = 300  # seconds (5 min)

    # Playback
    stream_url_resolve_timeout: int = 15  # seconds
    pipeline_start_timeout: int = 15  # seconds

    # Multi-room sync
    sync_poll_playing_interval: float = 3.0       # seconds, when groups are playing
    sync_poll_idle_interval: float = 10.0          # seconds, when no active groups
    sync_drift_threshold_ms: int = 500             # ms, correct if drift exceeds this
    sync_correction_cooldown_s: float = 15.0       # seconds, min between corrections per follower
    sync_dlna_default_buffer_s: float = 3.0        # seconds, default DLNA buffer delay
    dlna_settle_ms: int = 150                       # ms, delay after Stop before SetAVTransportURI
    dlna_play_delay_ms: int = 50                    # ms, delay after SetAVTransportURI before Play

    # Slow renderer support (Atoll ST300, etc.): some devices need extra time
    # between SetAVTransportURI and Play, plus retry logic if playback doesn't start.
    # Comma-separated substrings matched case-insensitively against device name/model/manufacturer.
    dlna_slow_renderer_patterns: str = "atoll,st300,st200,shangling,shanling,scd1"
    dlna_slow_startup_delay_ms: int = 1500          # ms, extra delay for slow renderers before Play
    dlna_slow_retry_timeout_ms: int = 3000          # ms, wait for position > 0 before retrying
    dlna_slow_max_retries: int = 2                  # max retry attempts for slow renderers

    # Multichannel / Surround
    downmix_policy: str = "auto"  # "auto", "stereo_always", "passthrough"
    downmix_matrix: str = ""  # Custom FFmpeg pan filter override for downmix
    surround_bass_management: bool = True  # Redirect bass from all channels to LFE
    surround_crossover_hz: int = 120  # Bass crossover frequency for LFE redirect

    # Chromecast
    chromecast_preload_secs: int = 20  # seconds before track end to QUEUE_INSERT next track
    chromecast_live_stream: bool = False  # experimental: concatenate tracks into a single live stream

    # Crossfade
    crossfade_enabled: bool = False
    crossfade_duration: float = 3.0  # seconds

    # Audio
    ffmpeg_path: str = Field(default_factory=lambda: _detect_bin("ffmpeg"))
    ffprobe_path: str = Field(default_factory=lambda: _detect_bin("ffprobe"))
    default_output_format: str = "flac"  # flac, wav, mp3, alac
    max_sample_rate: int = 192000
    max_bit_depth: int = 24

    # Resampling: "auto" (resample if needed), "never" (refuse incompatible),
    # "integer_ratio" (prefer 44.1→88.2→176.4, refuse non-integer ratios)
    resample_policy: str = "auto"
    # Audio buffer size in KB (per chunk)
    audio_buffer_kb: int = 32
    # Pre-buffer duration in seconds before starting playback
    prebuffer_seconds: float = 0.5

    # Local output: exclusive mode (bypass OS mixer for bit-perfect USB DAC)
    local_exclusive_mode: bool = False
    # Local output: latency preference in ms (lower = less latency, higher = more stable)
    local_latency_ms: int = 50

    # DSP / Processing chain
    dsp_enabled: bool = False
    # DSP filter file (FFmpeg -af format), e.g. "equalizer=f=1000:t=h:w=200:g=-3"
    dsp_filter: str = ""
    # Convolution impulse response file path (for room correction)
    dsp_impulse_response: str = ""
    # DSP sample rate (0 = use source rate)
    dsp_sample_rate: int = 0

    # Metadata
    metadata_readonly: bool = True  # When True, Tune never writes tags to audio files

    # When True, /metadata/fix-genres only assigns a genre that is already
    # present in the user's library (case-insensitive). Synonyms via
    # _GENRE_MAP still apply, but only if the canonical bucket is itself
    # in the existing vocabulary. Default False so a fresh install with
    # no genres yet still gets populated by Last.fm/Discogs tags.
    metadata_fix_genres_respect_vocabulary: bool = False

    # Plugin Store
    install_plugins: str = ""  # Comma-separated list of packages to auto-install at boot

    # Enrichment
    discogs_token: str = ""  # Personal Discogs API token for artist images
    lastfm_api_key: str = ""  # Last.fm API key for metadata enrichment
    lastfm_api_secret: str = ""  # Last.fm API shared secret for scrobbling
    lastfm_session_key: str = ""  # Last.fm session key (persisted after auth)
    lastfm_scrobble_enabled: bool = False  # Enable Last.fm scrobbling
    enrich_on_scan: bool = False  # Auto-enrich after library scan

    # Artist metadata (mozaiklabs.fr enrichment API)
    artist_metadata_url: str = "https://mozaiklabs.fr/api/v1/artists"
    artist_metadata_enabled: bool = True

    # Community shared artist image cache (mozaiklabs.fr)
    community_api_url: str = "https://mozaiklabs.fr/api/v1/artists"
    community_api_key: str = ""  # TUNE_COMMUNITY_API_KEY — shared across installations
    community_cache_enabled: bool = True

    # Artwork
    artwork_cache_dir: str = "artwork_cache"

    # Duplicates — moved here instead of deleted
    duplicates_dir: str = "/data/duplicates"
    artwork_max_size: int = 1200  # max dimension in pixels

    # Streaming API timeout (seconds) — applies to all connector HTTP calls.
    # Must be high enough for large Tidal playlist fetches (~34s for 280 playlists).
    api_timeout: int = 60

    # Auto-update: when true, the UpdateChecker downloads + installs new
    # releases without requiring a UI click, then restarts the process.
    # Skipped on Windows (running .exe is locked, can't be replaced in place).
    # Default off for safety; enable per-tester via TUNE_AUTO_UPDATE=true.
    auto_update: bool = False

    # Streaming services
    tidal_enabled: bool = False
    tidal_quality: str = "HI_RES_LOSSLESS"  # LOW, HIGH, LOSSLESS, HI_RES_LOSSLESS

    qobuz_enabled: bool = False
    qobuz_app_id: str | None = "798273057"
    qobuz_app_secret: str | None = "05a4851e74ee47fda346f50cfdfc4f09"

    # YouTube (Data API v3 + IFrame Player)
    youtube_enabled: bool = False
    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_api_key: str | None = None  # Optional: for unauthenticated search quota

    # Amazon Music
    amazon_music_enabled: bool = False
    amazon_music_region: str = "us"
    amazon_music_quality: str = "HD"  # SD, HD, ULTRA_HD

    # Spotify (catalogue connector — search/browse via Spotify Web API)
    spotify_enabled: bool = False
    spotify_client_id: str | None = None
    spotify_redirect_uri: str = "http://localhost:8888/api/v1/streaming/spotify/callback"

    # Spotify Connect receiver (Tune appears as a Spotify Connect device)
    spotify_connect_enabled: bool = False
    spotify_connect_zone_id: int | None = None  # zone that receives the audio
    spotify_connect_device_name: str | None = None  # default = hostname-based
    spotify_connect_bitrate: int = 320  # 96 / 160 / 320
    spotify_connect_binary: str | None = None  # path to librespot (default: PATH lookup)

    # Deezer
    deezer_enabled: bool = False
    deezer_arl: str | None = None
    deezer_quality: str = "FLAC"  # FLAC, MP3_320, MP3_128

    # Podcasts
    radiofrance_api_key: str | None = None  # Radio France Open API key

    # Discovery
    discovery_enabled: bool = True
    ssdp_enabled: bool = True
    mdns_enabled: bool = True
    cast_enabled: bool = True
    bluos_enabled: bool = True
    squeezebox_enabled: bool = True

    # Peer discovery (find other Tune servers on the LAN via mDNS)
    peer_discovery_enabled: bool = True
    server_name: str | None = None  # Friendly name for this server (defaults to hostname)

    # OpenHome renderers (Linn, Naim, Auralic, Lumin, dCS, etc.)
    openhome_auto_standby: bool = False  # put device to standby on stop
    openhome_queue_size: int = 20  # max tracks to push to device playlist

    # UPnP MediaServer
    upnp_server_enabled: bool = True
    upnp_server_name: str = "Tune Server"

    # Mode: "server" (default) or "remote" (proxy to another Tune Server)
    mode: str = "server"  # "server" | "remote"
    remote_host: str | None = None  # IP:port of the Tune Server to connect to
    remote_auto_discover: bool = True  # Auto-discover Tune Servers on LAN

    # Network shares & media servers
    network_shares_enabled: bool = False
    network_media_servers_enabled: bool = False
    smb_mount_dir: str = "~/.tune/mounts"
    smb_auto_discovery: bool = False
    smb_scan_interval: int = 60  # seconds between active SMB scans

    # Logging
    log_level: str = "INFO"
    log_format: str = "console"  # console or json


def _coerce_env_music_dirs() -> None:
    """Accept TUNE_MUSIC_DIRS=/path as shorthand for '["/path"]'."""
    import os
    raw = os.environ.get("TUNE_MUSIC_DIRS", "")
    if raw and not raw.lstrip().startswith("["):
        import json
        os.environ["TUNE_MUSIC_DIRS"] = json.dumps([raw])

_coerce_env_music_dirs()
settings = Settings()

def _log_env_files_loaded() -> None:
    """Log which .env files were found at startup."""
    import structlog
    _logger = structlog.get_logger()
    for candidate in _env_file_candidates():
        if Path(candidate).is_file():
            _logger.info("env_file_loaded", path=candidate)
    _logger.info("config_effective", quality_split=settings.quality_split,
                 db_engine=settings.db_engine, music_dirs=settings.music_dirs)

_log_env_files_loaded()


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

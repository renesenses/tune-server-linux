# Changelog

All notable changes to Tune Server.

## v0.2.2 — 2026-03-26

### Fixed
- **DLNA resilience**: automatic fallback to renderer monitor when the local pipeline breaks (e.g., network glitch) — playback continues seamlessly
- **DLNA resume**: pause/resume now works reliably on all DLNA renderers
- **Skip/seek reactivity**: previous track uses CD-style behavior (restart if >3s, else go back)
- **Track numbers**: streaming connectors (Tidal, Qobuz, YouTube) now correctly populate `track_number` and `disc_number`
- **Windows**: fixed crash on startup (`add_signal_handler` not supported on Win32)
- **PyInstaller 6+**: fixed `web/` directory detection inside `_internal/` bundle
- **Version detection**: fallback reads `pyproject.toml` when `importlib.metadata` is unavailable (frozen builds)

### Web Client
- **Full responsive UI**: 3 breakpoints — desktop (sidebar), tablet (icon sidebar), mobile (bottom tab bar)
- **Mobile bottom tab bar**: Zone selector, Home, Library, Search, Streaming, Plus (drawer with all remaining views)
- **Mini-player**: compact transport bar on mobile, tap to open full-screen Now Playing
- **Zone selector**: accessible on mobile and tablet via sheet overlay
- **Record button**: Now Playing view includes recording controls
- **Dynamic version**: no more hardcoded client version

---

## v0.1.6 — 2026-03-17

### Added
- **DLNA Media Server browsing**: discover and browse UPnP/DLNA media servers on the local network (Asset UPnP, Sonos, etc.) via `/network/media-servers` API
- **Media Server playback**: play tracks from DLNA media servers directly to any zone
- **Direct URL playback**: `file_path` parameter in PlayRequest and QueueAddRequest to play/queue media server streams and other direct URLs
- **Homebrew formula**: `brew install renesenses/tap/tune-server` for macOS (Apple Silicon + Intel) and Linux

### Fixed
- **Qobuz/Tidal skip on Micromega**: `supports_direct_url()` returned False for streaming services, forcing an unnecessary pipeline that conflicted with the proxy relay — tracks skipped every 1-2 seconds instead of playing
- **Play race condition**: stop pipeline before changing queue to prevent old `_direct_url_monitor` from advancing into the new queue

### Web Client
- **Media Servers view**: full browsing UI with breadcrumb navigation, format badges (FLAC 44.1kHz/16bit), duration, and add-to-queue button
- **Recently played fix**: media server albums now appear and are clickable — search by title fallback for tracks without album_id
- **Navigation fix**: clicking album title navigates to album page instead of starting playback
- **Harmonized track display**: media server tracks match library track layout (thumbnail, artist — album, format badge)

---

## v0.1.5 — 2026-03-14

### Added
- **Micromega M-One volume control**: proprietary protocol integration for native volume management
- **HTTPS→HTTP proxy**: transparent proxy for Tidal/Qobuz streams on DLNA renderers that don't support HTTPS
- **Native DSD on Micromega M-One**: automatic DSD passthrough detection and activation
- **Tag writing**: `PUT /library/tracks/{id}` and `PUT /library/albums/{id}` now write metadata (title, artist, album) to audio files via mutagen (FLAC, MP3, M4A, OGG)
- **Create artist endpoint**: `POST /library/artists` to create new artists
- **Hot add/remove music directories**: manage music directories via API without restart
- **Multi-room sync improvements**: adaptive polling (1s active / 10s idle), output position queries (DLNA GetPositionInfo, AirPlay metadata, local elapsed time), configurable sync parameters via environment variables
- **Per-zone sync offset**: `sync_delay_ms` field on zones for fine-tuning multi-room synchronization
- **Adaptive DLNA latency**: measure and cache actual renderer startup latency instead of fixed 3s delay
- **PATCH endpoint for zones**: partial update support for zone configuration
- **Web client**: Tune logo in sidebar, playing indicator on recently played, clickable streaming artists

### Fixed
- **Buffer alignment**: fix unaligned buffer causing all tracks to skip
- **Windows path normalization**: backslash→slash conversion for cross-platform compatibility
- **One device per zone**: prevent assigning the same device to multiple zones
- **Streaming artist source_id**: add source_id to Qobuz/Tidal artist responses
- **Radio HTTPS downgrade**: HTTPS→HTTP fallback for radio streams on renderers without TLS
- **Qobuz playlist pagination**: playlists with more than 50 tracks now fetch all items via pagination
- **Dynamic version**: read version from pyproject.toml instead of hardcoded string

### Changed
- Sync engine drift threshold reduced from 1000ms to 500ms (configurable)
- Sync engine correction cooldown reduced from 30s to 15s (configurable)
- Sync engine poll interval reduced from 5s to 1s when groups are active

---

## 2025-02-28

### Added
- **YouTube Music**: device code OAuth authentication and playlist support
- **Deezer**: OAuth 2.0 connector with search and featured content

## 2025-02-27

### Added
- **Spotify**: PKCE OAuth authentication connector

### Fixed
- SPA fallback middleware no longer shadows API routes

## 2025-02-25

### Added
- Radio station logo artwork for all 14 default stations
- Subdirectory scanning (scan a single configured music_dir)
- Backup/restore API endpoints with automatic pre-migration backups

### Fixed
- Audio hash computation handles PermissionError gracefully
- Track deduplication uses audio content MD5 hash

## 2025-02-23

### Added
- Streaming playlists for Tidal and Qobuz

### Fixed
- Track deduplication from multiple mount points
- Tidal playlist track ordering

## 2025-02-22

### Fixed
- DSF native playback: tracks no longer stop mid-track or fail to advance

## 2025-02-21

### Added
- **Native DSD/DSF passthrough** for DLNA renderers with auto-detection via GetProtocolInfo
- DSD detection fallback: device name/model heuristic for renderers without GetProtocolInfo
- Radio station cover upload endpoint

### Fixed
- FLAC streaming with total_samples=0 breaks DLNA renderers — use WAV instead
- FLAC encoder: use s32 for 24-bit (s24 is invalid for FFmpeg FLAC)
- High-quality DSD transcoding: 176.4kHz/24-bit WAV (44.1kHz family)

## 2025-02-20

### Added
- **Live radio stations**: CRUD API, M3U/PLS import, zone playback, genre filtering, favorites
- Per-directory rescan (scan a single mount point)

### Fixed
- Network mount auto-restore on startup
- Browse API shows device names instead of raw mount paths

## 2025-02-18

### Fixed
- DLNA direct URL passthrough for streaming services
- DIDL-Lite metadata passed correctly to DLNA renderers
- Qobuz stream URL signature uses float timestamp

## 2025-02-17

### Added
- Web client bundled in Debian package
- Library browse-by-directory endpoints
- **Network discovery**: SMB/NFS share discovery, mount management, DLNA MediaServer browsing

## 2025-02-15

### Added
- Metadata management: completeness stats, artwork upload/rescan, MusicBrainz enrichment
- Album cover art for all streaming sources (Qobuz, Tidal, YouTube)
- YouTube Music featured sections

### Fixed
- Tidal stream URL resolution, quality config, and fallback handling

## 2025-02-14

### Added
- Tidal featured sections from home page, disconnect endpoint
- Qobuz featured sections, disconnect, album cover art
- Streaming auth UI (Tidal OAuth + Qobuz login)
- Zone rename endpoint

## 2025-02-13

### Added
- Serve Svelte SPA from FastAPI (`TUNE_WEB_DIR`)
- Album merge-duplicates endpoint
- Local audio device listing
- MusicBrainz Cover Art Archive fallback

## 2025-02-12

### Added
- Ubuntu Server install guide for Mac Mini Late 2012
- `.deb` and Homebrew packaging

## 2025-02-11

### Added
- **Initial release**: fork music-server as tune-server
- FastAPI REST API (port 8888) + HTTP audio streamer (port 8080)
- Library scanner with mutagen metadata extraction
- SQLite database with FTS5 full-text search
- DLNA/UPnP output via async-upnp-client
- AirPlay output via pyatv
- Local soundcard output via sounddevice
- Multi-room zone grouping with sync engine
- Tidal and Qobuz streaming integration
- WebSocket real-time events
- Playlist CRUD
- Gapless playback with pre-buffering

# Changelog

All notable changes to Tune Server.

## v0.6.6 — 2026-04-19

### Streaming Services
- Enable/disable streaming services directly from the web UI (no more manual .env editing)
- Activating a service now persists all its config keys to .env (app_id, quality, region, etc.)
- All 6 services listed even when disabled, with Enable button

### Bug Fixes
- Fix DLNA startup hang: removed blocking 15s device wait loop (zones now go to pending and retry automatically)
- Fix ffmpeg detection on Windows (bundled .exe + platform-aware error hint)
- Fix `zones.output_device_id` FOREIGN KEY constraint error on zone creation
- Fix missing `list_backups()` method on SADatabase
- Fix Tidal featured sections going stale (30-min server-side cache + client pull-to-refresh)
- Fix "Bon après-midi, Default" greeting on iOS (hide Default profile name)
- Fix zone creation error feedback (visible alert instead of silent failure)

## v0.6.5 — 2026-04-16

### Zones — real-time device hot-unplug
- SSDP passive advertisement listener (alive/byebye) for DLNA — detection
  latency drops from ~30s polling to <1s when a renderer properly announces
  its lifecycle
- mDNS zeroconf passive browser for AirPlay (_raop / _airplay) — real-time
  `remove_service` triggers DEVICE_LOST immediately
- Zones auto-pause playback when their bound device goes offline and
  auto-resume on recovery (backoff 2s/5s/10s)
- ZONE_UPDATED events now carry `error_code: "device_unavailable"` and
  `resuming: true` so clients can badge zones correctly
- AirPlay output availability flag set consistently on timeout/error

### CI / Release infrastructure
- release.yml: wait up to 150s for GitHub to finalize asset uploads
  (sizes matching local artifacts) before pinging mozaiklabs.fr — fixes
  the partial 448 KB asset bug observed in v0.6.3/v0.6.4

## v0.6.4 — 2026-04-16

### Playlist Manager
- Playlist snapshots are now listable, restorable, and deletable — backup is no longer one-way
- Snapshot restore matches tracks against local library, creates or replaces a local playlist (409 confirm flow)
- Remote playlist creation on transfer: Tidal, Qobuz and Spotify now create the target playlist on the streaming service when `create_on_target=true` (Spotify scopes updated, re-auth required)
- Auto-sync scheduler: `playlist_links.sync_interval_minutes` is now honored by a background worker that ticks every 60s
- `PATCH /playlist-manager/links/{id}` to change direction or interval on an existing link
- Merge playlists UI in web client (multi-select + floating action bar, deduplicate toggle)

### Database
- `GET /system/database/export` (SQLite .db or PostgreSQL pg_dump)
- `POST /system/database/import` (validates magic bytes / applies via psql, creates safety backup first)

### Zones
- Hot-swap endpoint now actually works: `ZoneManager.set_output()` implemented with duplicate-device check, atomic output swap, event emission
- Zone `online` flag flips to false when the bound DLNA/AirPlay device goes offline (DEVICE_LOST event), and back to true on rediscovery

### Fixes
- SSDP discovery logs warning with fallback when device XML is unreachable
- AirPlay output availability now consistent on timeout/error
- Webhook release assets on mozaiklabs.fr: idempotent mkdir, fallback to `browser_download_url`, DB write wrapped in `lockForUpdate()` (fixes v0.6.2/v0.6.3 asset download race condition)

## v0.5.8 — 2026-04-12

### Metadata — Automatic Enrichment
- Auto-fix missing years via Tidal, MusicBrainz and Discogs (98% coverage)
- Auto-fix missing genres via Last.fm and Discogs (93.5% coverage)
- Fix PATCH metadata artist_name/album_title on PostgreSQL (auto-resolve to IDs)
- New endpoints: fix-years-tidal, fix-years-musicbrainz, fix-years-discogs, fix-years-tags, fix-genres
- Last.fm API key support

### Zone Manager
- Zone Manager view (web, iOS, Flutter)
- Per-zone mute/unmute, volume control, latency measurement
- Multi-room groups: create, rename, calibrate, dissolve
- Profiles: save and restore zone configurations
- DLNA device IP display, health monitoring, sync stats

### Metadata Manager (iOS + Flutter)
- Full metadata UI port to iOS/macOS and Flutter
- Action buttons: merge duplicates, enrich, fix years/genres, auto-fix albums
- Suggestions section with batch accept

### Fixes
- PostgreSQL zone_groups/zone_profiles tables
- INSERT OR REPLACE → ON CONFLICT for PostgreSQL
- Swift 6 Sendable: Codable structs for all API responses
- Zone manager overview (Pydantic model_dump, discovery IP lookup)

## v0.5.7 — 2026-04-12

### 🎵 Playlist Manager
- Cross-service transfer (Tidal/Qobuz/Deezer ↔ Local) with smart ISRC + fuzzy matching
- Batch transfer: all playlists from a service in one operation
- Merge playlists with deduplication
- Bidirectional sync (pull/push) with persistent links
- Export/Import: CSV, JSON, XSPF, Text
- Backup: snapshot all playlists with metadata
- History: log of all transfers with per-track detail
- Clickable filter badges on transfer results
- Drag & drop + ▲▼ reorder in local playlists (web)
- Context menu: Transfer / Duplicate / Delete (iOS long-press)
- Unified Playlists + Manager view (single sidebar entry)
- Tidal playlists cached 5min server-side (34s → instant)

### 🔊 Zone Manager
- Hot-swap device without recreating zone
- Persistent multi-room groups (survive restart)
- Profiles/Scenarios: save/recall zone configs + volumes
- Master volume + per-zone offsets in groups
- Per-zone mute within a group
- Sync <50ms: 100ms polling, progressive correction
- Latency measurement + auto-calibration
- Gapless multi-room: all-or-nothing coordination
- Health monitor: online/offline/degraded per zone

### 🏷️ Metadata Manager
- Manual edit: title, artist, album, genre, year, composer, ISRC, BPM, lyrics
- Tag writing: DB default, write to file on demand (Mutagen)
- Batch edit + global artist rename
- MusicBrainz lookup + Last.fm tags + Cover Art Archive
- AcoustID fingerprinting (batch identification)
- Auto-fix background scan with suggestions
- Auto-fix albums from file paths
- Duplicate detection (audio MD5 hash)
- Suggestions panel: accept/reject, accept-all ≥90%
- Cover embed into audio files (ID3/FLAC)

### 🎙️ Podcasts
- 120+ Radio France shows via Open API
- Station covers from iTunes (high-res)

### 📱 Native Apps
- Custom skip buttons |◀ ▶| across all views
- Heart button on mini player + now playing (radio + tracks)
- Clickable artist/album → detail views from Now Playing
- Parallel playlist loading with spinner
- Genres removed from sidebar
- Radio Favorites in web sidebar

### 🌐 Flutter (Android)
- Complete remote mode (API + WebSocket + all views)
- APK distributed via Firebase App Distribution

### 🔧 Fixes
- API timeout 15s → 60s (Tidal 280 playlists)
- PostgreSQL FTS: ILIKE fallback for French accents
- SSDP Windows: multicast retry with source IP
- Xcode archive stale cache fix
- FIP radio metadata in remote mode
- 903 artist images enriched from Discogs

---

## v0.5.5 — 2026-04-10

### Added
- **Apple Music (MusicKit)**: catalog search, playlists, album queue playback, transport controls (iOS/macOS)
- **CoreAudio device selection**: choose USB DAC, headphones, or any audio output on macOS
- **"Récemment ajouté"**: GET /library/albums/recent endpoint — albums sorted by creation date
- **Playlists tab**: Apple Music (201) + Tidal playlists with pagination (all playlists, not just 50)
- **Now Playing sidebar**: dedicated view in macOS sidebar
- **Signal path in now playing bar**: visible without opening the sheet
- **Local audio devices**: Sources & Devices shows server audio outputs (Linux/Windows via API)
- **YouTube Music**: enabled on server, authenticated without OAuth (yt-dlp)
- **Deezer**: added to streaming services list
- **Firebase App Distribution**: Android beta testing via Firebase

### Fixed
- **Signal path**: "Lossy" for AAC/MP3 instead of misleading "Transcoded"
- **Radio playback**: HTTPS for local zones, HTTP only for DLNA renderers (ATS fix)
- **Radio format**: detect AAC/MP3 from stream URL for correct signal path display
- **Search navigation**: programmatic navigation fixes .searchable() interference on iPhone
- **Local zone persistence**: zone (id=-1) no longer disappears after syncZoneState
- **Default zone**: prefer server zones over local on connect
- **Apple Music controls**: play/pause/next/prev route to ApplicationMusicPlayer
- **Apple Music artwork**: filter internal musicKit:// URLs, use catalog HTTPS lookup
- **Album detail**: handle nil album ID + empty tracks gracefully
- **Tidal pagination**: load all playlists (was limited to 50)
- **Artwork fallback**: resolve cover paths from settings when API not initialized
- **Search padding**: bottom margin for now playing bar
- **Zone name**: use actual device name/model (iPhone not iPad)

### Infrastructure
- Servers .29 (.18) and .50 deployed with sudoers NOPASSWD
- Web client built and deployed
- Radio logos restored on all servers

## v0.5.4 — 2026-04-08

### Fixed
- **DLNA playback stability**: fixed pipeline conflict causing tracks to cut after 10-30s on DMP-A8 and other renderers — pipeline now stops when HTTP streamer serves files directly
- **DLNA STOPPED debounce**: require 3 consecutive STOPPED polls before advancing track, prevents premature skip during renderer buffering
- **DLNA polling interval**: reduced UPnP GetPositionInfo from 1s to 10s, prevents audio dropouts on sensitive renderers
- **Radio stream stability**: disabled position polling during radio playback (no need to monitor indefinite streams)
- **Radio favorites crash**: initialized RadioFavoriteRepo (was None, causing ASGI errors on every ICY metadata update)
- **Windows crash**: removed `reuse_port` socket option not supported on Windows
- **HTTP stream headers**: disabled non-standard X-Tune-* headers that caused some DLNA renderers to drop connections
- **DLNA Stop before Play**: limited pre-play Stop command to Micromega only (broke DMP-A8 with 30s timeout)
- **PLAYBACK_TRACK_CHANGED**: now emitted on skip_next/skip_previous for reliable WebSocket transport bar updates
- **systemd port cleanup**: ExecStartPre/ExecStopPost kill port 8888 zombies on restart

### Added
- **PATCH /api/v1/system/config**: runtime toggle for metadata_readonly and enrich_on_scan
- **Signal Path in iOS/iPadOS/macOS apps**: bit-perfect/transcoded pill in NowPlaying with detail sheet
- **Embedded HTTP server (iPadOS)**: full REST API on port 8888 when running in server mode (NWListener, zero dependencies)
- **WebSocket support (iPadOS)**: real-time event broadcast matching Python server format
- **Progress bar**: elapsed/total time with click-to-seek in web client transport bar
- **HeartButton**: favorites on artist detail album cards (web client)

### Web Client
- **Progress bar** in transport bar with elapsed/total time and click-to-seek
- **HeartButton** on artist detail view album cards

## v0.5.3 — 2026-04-07

### Added
- **Metadata Readonly Mode**: `TUNE_METADATA_READONLY` — Tune never writes tags to audio files when enabled
- **Auto Artist Image Enrichment**: Discogs integration fetches artist photos after library scan
- **Enrichment UI**: Settings section with Discogs token status and manual "Enrich Now" button
- **systemd KillMode=mixed**: clean process termination on restart (no more zombie ports)

### Fixed
- **EventBus.on()**: fixed subscribe call that prevented auto-enrichment after scan
- **Create Zone (iPhone)**: replaced buggy alert TextField with proper sheet form

### Web Client
- **Metadata readonly toggle** in Settings
- **Enrichment section** with Discogs token status + enrich button

## v0.5.2 — 2026-04-07

### Added
- **Signal Path Display**: Roon-style modal showing complete audio chain (source → transport → output) with bit-perfect indicator
- **Bit-Perfect Verification**: MD5 checksum on passthrough, pipeline decision log
- **Precision Timing Headers**: X-Tune-SampleRate, BitDepth, Timestamp (ns), BitPerfect on all HTTP streams
- **Resampling Policy**: auto, never, integer_ratio — configurable via Settings
- **ALSA Exclusive Mode**: bypass OS mixer for bit-perfect USB DAC output, configurable latency
- **DSP Processing Chain**: FFmpeg filter support (EQ, convolution), impulse response for room correction
- **Audio Quality Settings UI**: new section in Settings for all audio parameters

### Fixed
- **Port 8080 zombie**: reuse_address + reuse_port on HTTP streamer, systemd KillMode=mixed
- **Radio cover in playback**: station logo persists after ICY metadata update
- **Playlists loading indicator**: spinner visible during streaming playlists fetch (was in wrong component)

### Web Client
- **Signal path dot**: in transport bar controls, green (bit-perfect) / yellow (transcoded)
- **Signal path modal**: Roon-inspired card with circular icons, color-coded lines, expandable decisions
- **Checksum verified badge**: displayed in signal path modal
- **Full FR/EN i18n**: all signal path and audio settings translated

## v0.5.1 — 2026-04-06

### Fixed
- **Tidal playlists pagination**: fetch all own playlists + favorites (was limited to 50)
- **Track edit genre/year**: fields now correctly update album table instead of track table
- **Playlists loading indicator**: streaming playlists loading bar visible during fetch

### Added
- **Track metadata editing**: genre, year fields with write-through to file tags (FLAC/MP3/MP4/Ogg)

### Web Client
- **Playlist loading bar**: spinner with progressive counter while streaming playlists load
- **Track edit improvements**: custom dropdown for artist/album, genre/year fields, no-crash on paste
- **Heart button fixes**: always visible (opacity 0.4), present on both single-disc and multi-disc albums
- **Transport bar**: triple refresh after playlist play to ensure state updates

## v0.5.0 — 2026-04-05

### Added
- **User Profiles & Favorites**: multi-user profiles (Netflix-style), favorites for tracks/albums/artists per profile, heart buttons across all views
- **Playlist Manager**: unified cross-service playlist view, import from streaming, transfer between services with fuzzy track matching, playlist diff/comparison, track availability recovery
- **PostgreSQL Support**: full dual-engine support (SQLite + PostgreSQL), migration tool, tsvector FTS, connection pooling
- **Album Filters**: filter by format (FLAC, WAV, MP3, DSD) and sample rate (44.1kHz+, 96kHz+, 192kHz+)
- **Enhanced Metadata**: batch genre/year assignment, MusicBrainz enrichment, genre normalization (Discogs-style), extended tag writing (genre, year, albumartist, disc/track number)
- **Database Settings UI**: engine badge, connection status, pool size in Settings

### Improved
- **DLNA Native Seek**: instant seek (<1s) via Seek(REL_TIME) when renderer supports it
- **Artwork Performance**: thumbnail generation (?size=200), browser cache headers, lazy loading
- **Browse Directories**: PostgreSQL-compatible directory browsing (SUBSTR/SPLIT_PART)
- **Genre Cleanup**: 42 messy genres normalized to 15 clean Discogs-style categories

### Web Client
- **Profile Selector**: sidebar avatar, create/switch/delete profiles
- **Favorites View**: dedicated section with tabs (tracks/albums/artists)
- **Heart Buttons**: on album cards and track rows, with favorites filter chip
- **Playlist Manager**: unified view, import dialog, transfer with report, diff comparison, recovery checker
- **Metadata**: merge duplicates, batch operations, MusicBrainz enrich button, genre autocomplete
- Renamed "Maintenance" → "Metadata"

## v0.3.1 — 2026-04-04

### Fixed
- **Startup crash**: fixed AttributeError on startup that affected v0.3.0 binary builds
- **Library search**: searching by artist name now returns their albums and tracks

### Web Client
- **Artists pagination**: library view now loads all artists instead of truncating at 500

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

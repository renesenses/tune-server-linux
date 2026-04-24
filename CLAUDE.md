# CLAUDE.md — tune-server-linux

## CRITICAL RULES

**NEVER mention "recorder", "recording", "special-edition", or "tune-special-edition" in this repo or any public context (GitHub releases, changelogs, forum posts, commit messages). This is the PUBLIC repo.**

If you encounter recording-related code (event types, routes, models), do NOT reference it in any public-facing output.

---

## Project Overview

Tune Server is an open-source multi-room music server written in Python. It manages a local audio library, connects to streaming services (Tidal, Qobuz, YouTube Music, Spotify, Deezer, Amazon Music), and streams to DLNA/UPnP renderers, AirPlay devices, or local soundcards. REST API on port 8888, UPnP MediaServer on port 8080, WebSocket for real-time events.

Current version: defined in `pyproject.toml` (`[project] version`).

## Architecture

```
tune_server/
├── api/              # FastAPI routes + WebSocket
│   ├── routes/       # library, playback, zones, streaming, playlists, radios,
│   │                 # search, devices, network, system, artist_metadata
│   ├── websocket.py  # WS push with pattern-based filtering
│   └── deps.py       # Dependency injection (AppDeps container)
├── audio/            # Pipeline: decoder -> resampler -> encoder -> buffer
│   ├── pipeline.py   # FFmpeg-based transcode/passthrough pipeline
│   ├── formats.py    # DLNA capabilities, DSD detection, MIME types
│   └── buffer.py / decoder.py / encoder.py / resampler.py
├── db/               # SQLite (aiosqlite) + PostgreSQL (asyncpg/SQLAlchemy)
│   ├── engine.py     # SQLite database wrapper
│   ├── sa_engine.py  # SQLAlchemy async engine (PostgreSQL + SQLite)
│   ├── repository.py # Repos: ArtistRepo, AlbumRepo, TrackRepo, TrackCreditRepo, etc.
│   ├── sa_repository.py # SQLAlchemy-based repository (Windows/PostgreSQL)
│   ├── tables.py     # SQLAlchemy table definitions
│   └── schema_sqlite.sql # Full schema with FTS5 virtual tables + sync triggers
├── discovery/        # SSDP (DLNA) + mDNS (AirPlay) + network shares + media servers
├── library/          # Scanner, watcher (watchfiles), metadata (mutagen),
│                     # artwork (MusicBrainz), enrichment
├── metadata/         # Artist enrichment (MusicBrainz + Last.fm + Wikipedia + Claude AI)
├── metadata_manager/ # Track/album metadata: enricher, matcher, fingerprint,
│                     # cover fetcher, auto-fix, credit enricher (MusicBrainz instruments)
├── outputs/          # Output targets
│   ├── dlna.py       # DLNA/UPnP renderer output (async-upnp-client)
│   ├── airplay.py    # AirPlay output (pyatv) with exponential backoff retry
│   ├── local.py      # Local soundcard (sounddevice)
│   └── http_streamer.py  # HTTP stream serving for DLNA
├── playback/         # Player state machine, queue, gapless pre-buffering
├── streaming/        # Service connectors: tidal, qobuz, youtube, spotify,
│                     # deezer, amazon, radio_metadata
├── zones/            # Multi-room: zone instances, groups, sync engine, stereo pairing
├── upnp_server/      # UPnP MediaServer (SSDP advertiser + ContentDirectory)
├── remote/           # Proxy mode: discover + relay to another Tune Server
├── network/          # SMB/NFS mount manager (macOS/Linux/Windows), media server browser
├── utils/            # network.py (IP detection with Windows ipconfig fallback)
├── event_bus.py      # Async pub/sub (40+ event types)
├── models.py         # Pydantic models (Track, Album, Artist, Zone, TrackCredit, etc.)
├── config.py         # Settings via pydantic-settings, env prefix TUNE_
└── __main__.py       # Entry point: asyncio.run(run_server())
```

## Key Commands

```bash
# Install dependencies (requires Python >= 3.11)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"

# Run the server
python -m tune_server
# or: tune-server (after pip install)

# Run tests
pytest                    # all tests
pytest tests/test_scanner.py  # single file
pytest -x                 # stop on first failure

# Configuration via environment or .env file (prefix: TUNE_)
TUNE_MUSIC_DIRS='["/path/to/music"]'
TUNE_DB_PATH="tune_server.db"
TUNE_API_PORT=8888
TUNE_LOG_LEVEL=DEBUG
```

## Build & Release

Release workflow (`.github/workflows/release.yml`) builds PyInstaller binaries triggered by `v*` tags or `workflow_dispatch`.

**Windows build MUST include:** `tune-server.exe`, `ffmpeg.exe`, `ffprobe.exe`, `start-tune-server.bat`. These are bundled in the workflow — never remove the FFmpeg download step or bat creation.

```bash
git tag v0.2.3 && git push origin v0.2.3
```

This triggers GitHub Actions which:
1. Builds archives for linux, macos, windows
2. Extracts changelog section for the version
3. Creates a GitHub Release with the archives + SHA256 checksums

There is also `build-linux.yml` for Linux-only builds.

### Linux installation
```bash
sudo ./install.sh [--systemd]
# Installs to /opt/tune-server, creates service user, sets up venv
```

## Deployment Targets

- **`.18`** — `192.168.1.18` — Primary Tune Server (Mac mini "roon")
- **`.15`** — `192.168.1.15` — Secondary Tune Server (Linux, special-edition)

Deploy by git pull + systemctl restart, or uploading the release archive and running install.sh.

## Database

Dual engine: **SQLite** via `aiosqlite` (default, Windows) or **PostgreSQL** via `asyncpg`/SQLAlchemy (production .18).

Schema: `db/schema_sqlite.sql` (SQLite) / `db/tables.py` (SQLAlchemy). Engine selected via `TUNE_DB_ENGINE=postgres` + `TUNE_DB_URL`.

**Tables**: artists, albums, tracks, playlists, playlist_tracks, zones, play_queue, streaming_auth, radio_stations, radio_favorites, network_mounts, device_credentials, user_profiles, user_favorites, track_credits, metadata_suggestions, metadata_fix_reports, duplicate_tracks, zone_groups, zone_group_members, zone_profiles, playlist_links, playlist_snapshots, transfer_history, sync_schedules

**Track Credits**: `track_credits` table — multiple artists per track with role (performer/composer/conductor/lyricist) and instrument. Populated from audio tags (PERFORMER/TMCL/TIPL) during scan + enriched from MusicBrainz artist data.

**FTS5** virtual tables for full-text search: tracks_fts, albums_fts, artists_fts (with auto-sync triggers). Search also queries artist name, genre, year, composer, instrument, label via LIKE fallback.

**Repository pattern** (GRDB-style): `ArtistRepo`, `AlbumRepo`, `TrackRepo`, `TrackCreditRepo`, `PlaylistRepo`, `PlayQueueRepo`, `ZoneRepo`, `RadioStationRepo`, `RadioFavoriteRepo` in `db/repository.py`. All queries are parameterized async methods. SA variants in `db/sa_repository.py`.

## Technical Notes

### DLNA Quirks (Micromega M-One)
- **Must send explicit Stop before SetAVTransportURI** — some renderers (Micromega) ignore a new URI without a preceding Stop command. See `dlna.py` line ~177.
- **HTTPS downgrade**: Micromega does not support HTTPS. Streaming URLs (Tidal, Qobuz) are downgraded to HTTP or proxied.
- **Proprietary volume**: Micromega uses HTTP on port 7000 for volume control (not standard DLNA volume).

### Quality-Based Album Splitting
The scanner (`library/scanner.py`) splits albums by quality tier. If files for the same album title have different sample rates (e.g., 44.1kHz vs 96kHz), separate album entries are created with a suffix like "Album Name (96kHz/24bit)". See `_same_quality_tier()` and `_quality_suffix()`.

### Audio Pipeline
FFmpeg-based: decoder -> optional resampler -> encoder -> ring buffer. Supports bit-perfect passthrough, native DSD (DSF/DFF) to capable renderers, PCM fallback at 176.4kHz/24-bit. Channel filter for stereo pairing (pan=mono|c0=FL/FR).

### Event Bus
Async pub/sub in `event_bus.py`. 40+ event types covering library, playback, playlists, zones, discovery, streaming. WebSocket clients can subscribe with pattern-based filtering.

### Windows Support
- PyInstaller build with `--noconsole` (stdout/stderr redirected to devnull)
- `get_local_ip()` falls back to `ipconfig` parsing for multi-NIC setups
- SMB discovery via `net view` (Linux uses `smbclient`, macOS uses `smbutil`)
- SQLite `PRAGMA busy_timeout=5000` to avoid SQLITE_BUSY under concurrent access
- AirPlay requires Apple Bonjour (included with iTunes) for mDNS discovery

### Track Credits
Multiple artists per track with roles and instruments. Extracted from audio tags during scan (PERFORMER, TMCL, TIPL, COMPOSER, CONDUCTOR). Enriched from MusicBrainz artist data via `POST /library/albums/{id}/credits/enrich` or `POST /library/enrich-credits` (all).

## Related Repos

| Repo | Description |
|------|-------------|
| `tune-special-edition` | **Private**. Do NOT cross-reference publicly. |
| `tune-server-ipados` | Native Swift/SwiftUI server + client for iPadOS/iOS (schemes: Tune_iOS, Tune_macOS) |
| `tune-web-client` | Svelte 5 SPA (embedded in `web/` at build time) |
| `tune-server-flutter` | Flutter cross-platform client (iOS/Android) |
| `site-mozaiklabs` | mozaiklabs.fr website — Laravel/Docker, deploy via `docker cp` (NOT build in container) |

## Web Client

The `web/` directory contains the built Svelte SPA (static assets). The server serves it automatically when `TUNE_WEB_DIR` is set or auto-detected. The source lives in `tune-web-client`.

### Search
Library search supports multi-term queries matching across: title, artist name, genre, year, composer, instrument, label. FTS5 for title + LIKE fallback for other fields.

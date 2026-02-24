# Phase 1 — Foundations & Streaming Playback

**Date:** 12 février 2026
**Commits:** `771117f` → `df84eb8` (7 commits)
**Tests added:** 172

## Objectives

Build the complete music server from scratch:
- Multi-room audio server with REST API
- Local library scanning with full-text search
- Audio pipeline with format detection and passthrough
- DLNA, AirPlay, and local audio outputs
- Tidal and Qobuz streaming integration
- Device discovery (SSDP + mDNS)
- Real-time event system with WebSocket

## Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Web framework | FastAPI + Uvicorn | Async-native, OpenAPI auto-docs, WebSocket support |
| Database | SQLite + aiosqlite | Embedded, zero-config, WAL mode for concurrent reads |
| Search | FTS5 virtual tables | Built into SQLite, supports prefix/phrase/boolean queries |
| Audio decode | FFmpeg subprocess | Universal format support, async pipe I/O |
| Event system | Custom async pub/sub | Lightweight, no external dependency, fits single-process model |
| Config | Pydantic Settings | Type-safe, env var loading, `.env` file support |
| DLNA control | async-upnp-client | Mature, async, handles SOAP/eventing |
| AirPlay | pyatv | Official Apple protocol support |
| Metadata | mutagen | Supports all major audio formats |
| Logging | structlog | Structured JSON logging, context binding |

## Changes by Module

### Core (`tune_server/`)
- `app.py` — Main `TuneServer` class with startup/shutdown orchestration
- `config.py` — Pydantic Settings with `TUNE_` prefix, env loading
- `models.py` — Domain models: Track, Album, Artist, Zone, SearchResult, request/response models
- `event_bus.py` — `EventBus` with `on()`, `on_all()`, `emit()`, `emit_nowait()`, 24 event types

### Database (`db/`)
- `schema.sql` — 7 tables (artists, albums, tracks, playlists, playlist_tracks, zones, play_queue) + 3 FTS tables + triggers
- `engine.py` — Database connection, WAL mode, foreign keys, schema initialization
- `repository.py` — Repository pattern: ArtistRepo, AlbumRepo, TrackRepo, PlayQueueRepo, ZoneRepo

### Library (`library/`)
- `scanner.py` — Recursive directory scanner with mutagen metadata extraction
- `watcher.py` — Filesystem watcher (watchfiles) with debounce
- `metadata_reader.py` — Format detection, tag extraction for FLAC/MP3/AAC/ALAC/OGG/OPUS/DSD/AIFF/WMA
- `artwork.py` — Album art extraction and caching
- `enrichment.py` — MusicBrainz metadata lookup (background)

### Audio (`audio/`)
- `pipeline.py` — Async audio pipeline with passthrough/decode strategy
- `decoder.py` — FFmpeg async subprocess with stdin/stdout pipes
- `encoder.py` — PCM encoding for output targets
- `buffer.py` — Thread-safe ring buffer for audio data
- `formats.py` — Audio format definitions, capability matching
- `resampler.py` — Sample rate conversion via FFmpeg soxr

### Playback (`playback/`)
- `player.py` — State machine (stopped → buffering → playing → paused)
- `queue.py` — Play queue with shuffle/repeat support

### Outputs (`outputs/`)
- `base.py` — `OutputTarget` abstract protocol
- `dlna.py` — DLNA renderer control (SetTransportURI, Play, Pause, etc.)
- `airplay.py` — AirPlay output via pyatv
- `local.py` — Local soundcard via sounddevice + numpy
- `http_streamer.py` — HTTP audio server for DLNA pull-streaming

### Zones (`zones/`)
- `zone.py` — `ZoneInstance` combining player + queue + output
- `manager.py` — `ZoneManager` for zone lifecycle
- `group.py` — `ZoneGroup` for synchronized multi-room playback
- `sync.py` — `SyncEngine` for position synchronization

### Streaming (`streaming/`)
- `base.py` — `StreamingService` abstract base class
- `tidal.py` — Tidal integration (tidalapi, OAuth device flow)
- `qobuz.py` — Qobuz integration (aiohttp, app_id/secret auth)
- `cache.py` — `StreamUrlCache` with TTL expiry

### Discovery (`discovery/`)
- `manager.py` — `DiscoveryManager` coordinating SSDP + mDNS
- `mdns.py` — mDNS/Bonjour discovery (zeroconf)
- `ssdp.py` — SSDP/UPnP discovery (async-upnp-client)

### API (`api/`)
- `main.py` — FastAPI app creation, router mounting, CORS, lifespan
- `deps.py` — `AppDeps` dependency injection container
- `websocket.py` — `WebSocketManager` for event broadcasting
- `routes/library.py` — Library CRUD + search endpoints
- `routes/playback.py` — Zone playback control endpoints
- `routes/zones.py` — Zone management endpoints
- `routes/devices.py` — Device discovery endpoints
- `routes/streaming.py` — Streaming service auth/search endpoints
- `routes/system.py` — Health, stats, scan trigger

### Utils (`utils/`)
- `audio_utils.py` — FFmpeg availability check, audio helpers
- `network.py` — Local IP detection for DLNA callback URLs

## Files Created

| File | Lines | Description |
|------|-------|-------------|
| `tune_server/app.py` | ~220 | Application orchestrator |
| `tune_server/config.py` | ~86 | Configuration settings |
| `tune_server/models.py` | ~250 | Domain models |
| `tune_server/event_bus.py` | ~100 | Event system |
| `tune_server/db/schema.sql` | ~120 | Database schema |
| `tune_server/db/engine.py` | ~50 | DB connection |
| `tune_server/db/repository.py` | ~400 | 5 repositories |
| `tune_server/library/scanner.py` | ~200 | Library scanner |
| `tune_server/audio/pipeline.py` | ~150 | Audio pipeline |
| `tune_server/playback/player.py` | ~250 | Player state machine |
| `tune_server/playback/queue.py` | ~150 | Play queue |
| `tune_server/zones/manager.py` | ~200 | Zone manager |
| `tune_server/streaming/tidal.py` | ~300 | Tidal service |
| `tune_server/streaming/qobuz.py` | ~300 | Qobuz service |
| `tune_server/api/routes/*.py` | ~500 | API routes |
| + 30 more files | | |

## Tests

- `test_repository.py` — Database CRUD operations
- `test_event_bus.py` — Pub/sub, emit, subscribe/unsubscribe
- `test_player.py` — State transitions, error handling
- `test_queue.py` — Add, remove, shuffle, repeat
- `test_buffer.py` — Ring buffer read/write, overflow
- `test_formats.py` — Format detection, capability matching
- `test_scanner.py` — File discovery, metadata extraction
- `test_metadata_reader.py` — Tag parsing for multiple formats
- `test_models.py` — Pydantic model validation
- `test_api_library.py` — Library endpoint integration tests
- `test_api_playback.py` — Playback endpoint tests
- `test_api_zones.py` — Zone management tests
- `test_websocket.py` — WebSocket connection, event broadcast
- `test_discovery.py` — SSDP/mDNS mock tests
- `test_http_streamer.py` — HTTP streaming sessions
- `test_stream_cache.py` — URL cache TTL

**Total: 172 tests, 91% coverage on tested modules**

## Commits

| Hash | Message |
|------|---------|
| `771117f` | Initial commit: Tune Server |
| `45029eb` | Remove SQLite shared memory file from tracking |
| `f729aca` | Add README and update .gitignore |
| `7f2091d` | Add architecture documentation |
| `df84eb8` | Add comprehensive test suite (172 tests, 91% coverage on tested modules) |

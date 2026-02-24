# Phase 5 — YouTube Music, Amazon Music, Typed Responses & Graceful Shutdown

**Date:** 13 février 2026
**Commit:** `aa5e64b`
**Tests added:** ~70

## Objectives

- Add YouTube Music streaming integration
- Add Amazon Music streaming integration
- Create streaming service registry for dynamic service management
- Add typed API responses (Pydantic response models)
- Implement graceful shutdown with per-component timeouts

## Changes

### YouTube Music Integration (`streaming/youtube.py`)
- Uses `ytmusicapi` for catalog browsing (search, albums, artists, tracks)
- Uses `yt-dlp` for stream URL extraction (audio-only best quality)
- OAuth file-based authentication (`TUNE_YOUTUBE_OAUTH_JSON`)
- Interactive OAuth device flow for initial setup
- URL cache with configurable TTL (`TUNE_YOUTUBE_URL_CACHE_TTL`, default 3600s)
- Same `StreamingService` interface as Tidal/Qobuz

### Amazon Music Integration (`streaming/amazon.py`)
- Undocumented Amazon Music API
- OAuth device flow with polling for authentication
- Device-specific auth (UUID-based device identity)
- Refresh token support for session persistence
- Quality tiers: SD (AAC 256kbps), HD (FLAC 16/44.1), ULTRA_HD (FLAC 24/192)
- Region-aware endpoints (`TUNE_AMAZON_MUSIC_REGION`)
- URL cache with 600s TTL (CDN URLs expire ~30min)

### Streaming Service Registry
- `deps.streaming_services: dict[str, StreamingService]` in AppDeps
- Dynamic registration based on `*_ENABLED` config flags
- `GET /streaming/services` — List all services with authentication status
- Auth restore on startup for all enabled services
- Graceful handling of unavailable services (logged, not fatal)

### Typed API Responses
- All endpoints return typed Pydantic response models
- `SystemHealthResponse`, `SystemStatsResponse`, `ScanStatusResponse`, `SystemConfigResponse`
- Consistent error format across all endpoints
- FastAPI auto-generates accurate OpenAPI schema

### Graceful Shutdown
- `_safe_stop(coro, name, timeout)` helper for per-component shutdown
- Each component has its own timeout (prevents one hung component from blocking shutdown)
- Shutdown order: enricher → watcher → sync → websocket → discovery → zones → streamer → streaming → db
- Streaming services: `close()` method saves auth state, closes HTTP sessions
- Zones: playback stopped, outputs closed, state persisted

## Technical Decisions

| Decision | Rationale |
|----------|-----------|
| yt-dlp for YouTube URLs | Only reliable way to extract playable audio URLs from YouTube |
| Undocumented Amazon API | No official SDK for Amazon Music; reverse-engineered protocol |
| Service registry pattern | Uniform handling of 2→4 services; easy to add more |
| Per-component shutdown timeout | Prevents cascade failures during shutdown |
| UUID device identity for Amazon | Amazon requires unique device ID per client |

## Files Created/Modified

| File | Change |
|------|--------|
| `streaming/youtube.py` | **Created** — YouTube Music service |
| `streaming/amazon.py` | **Created** — Amazon Music service |
| `config.py` | Added YouTube and Amazon config: `YOUTUBE_ENABLED`, `YOUTUBE_OAUTH_JSON`, `YOUTUBE_URL_CACHE_TTL`, `AMAZON_TUNE_ENABLED`, `AMAZON_TUNE_REGION`, `AMAZON_TUNE_QUALITY` |
| `api/deps.py` | Streaming service registry setup |
| `app.py` | `_safe_stop()` helper, updated startup (YouTube/Amazon auth restore), updated shutdown sequence |
| `api/routes/streaming.py` | `GET /streaming/services` endpoint |
| `api/routes/system.py` | Typed response models |
| `models.py` | Response models: `SystemHealthResponse`, `SystemStatsResponse`, `SystemConfigResponse`, `ScanStatusResponse` |

## Tests

- `test_phase5.py` — ~70 tests covering:
  - YouTube Music: search, get_track, get_album, get_stream_url, auth flow
  - Amazon Music: search, quality tiers, region endpoints, device auth
  - Service registry: list services, status, enable/disable
  - Typed responses: validate all response model fields
  - Graceful shutdown: component order, timeout handling
  - Auth restore: save/load credentials from DB
  - URL cache: TTL expiry, cache hit/miss

## Commit

| Hash | Message |
|------|---------|
| `aa5e64b` | Add streaming service registry, YouTube/Amazon Music, typed API responses, and graceful shutdown |

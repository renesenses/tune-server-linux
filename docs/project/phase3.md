# Phase 3 — Security & Robustness

**Date:** 12-13 février 2026
**Commit:** `727fc1a`
**Tests added:** ~50

## Objectives

- Add security layer (API key authentication, CORS)
- Harden input validation to prevent injection and abuse
- Improve connection resilience for network outputs (DLNA, AirPlay)
- Fix thread safety issues identified in review

## Changes

### API Key Authentication
- Optional `X-API-Key` header authentication
- Configured via `TUNE_API_KEY` environment variable
- When set, all API requests must include the header
- When `None` (default), no authentication required
- Health endpoint (`/system/health`) exempt from auth

### CORS Configuration
- `TUNE_CORS_ORIGINS` setting (list of allowed origins)
- Default: `["*"]` (open, for development)
- Production: restrict to specific origins

### Input Validation Hardening
- Pagination: `offset >= 0` enforced, `limit` capped at reasonable maximum
- Path traversal prevention on artwork and file-serving endpoints
- Search query sanitization for FTS5 injection prevention
- Request body size limits

### Connection Resilience
- DLNA output: automatic reconnection on transport error
- AirPlay output: retry with backoff on auth failure
- HTTP streamer: graceful handling of client disconnects (`ClientConnectionResetError`)
- Zone marked as degraded (not deleted) when output becomes unavailable

### Thread Safety
- Event bus: guard against subscriber modification during iteration
- WebSocket manager: connection set protected against concurrent modification
- Scanner: lock to prevent concurrent scan operations
- Discovery: thread-safe device registry updates

## Technical Decisions

| Decision | Rationale |
|----------|-----------|
| API key (not JWT/OAuth) | Simple, sufficient for single-user/home use; no user management needed |
| CORS as config | Allows same binary for dev (open) and prod (restricted) |
| Offset validation | Prevents negative offset causing SQL errors or unexpected results |
| Reconnect (not recreate) | Preserves zone state and queue on transient network failures |

## Files Modified

| File | Change |
|------|--------|
| `api/main.py` | Added API key middleware, CORS configuration |
| `config.py` | Added `TUNE_API_KEY`, `TUNE_CORS_ORIGINS` |
| `api/routes/library.py` | Input validation on pagination params |
| `api/routes/streaming.py` | Validation on search params |
| `library/artwork.py` | Path traversal check |
| `outputs/dlna.py` | Reconnection logic on transport error |
| `outputs/airplay.py` | Auth retry with backoff |
| `outputs/http_streamer.py` | ClientConnectionResetError handling |
| `event_bus.py` | Subscriber list copy during iteration |
| `api/websocket.py` | Connection set thread safety |
| `library/scanner.py` | Scan lock enforcement |
| `discovery/manager.py` | Thread-safe device updates |

## Tests

- `test_phase3.py` — ~50 tests covering:
  - API key middleware (valid key, missing key, wrong key)
  - CORS headers on responses
  - Pagination validation (negative offset, excessive limit)
  - Path traversal attempts on artwork endpoint
  - FTS5 query sanitization
  - DLNA reconnection after transport error
  - AirPlay auth retry
  - Concurrent scan prevention
  - WebSocket concurrent connection handling

## Commit

| Hash | Message |
|------|---------|
| `727fc1a` | Add security hardening, connection resilience, and reliability improvements |

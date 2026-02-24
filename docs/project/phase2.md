# Phase 2 — Bug Fixes & Gapless Playback

**Date:** 12 février 2026
**Commits:** `a5af25c` → `57562cb` (4 commits)
**Tests added:** 151

## Objectives

- Fix critical bugs discovered in Phase 1 (AirPlay auth, scanner race condition)
- Add gapless playback for seamless track transitions
- Implement playlist API (CRUD + track management)
- Add metadata editing endpoints
- Enable streaming playback (URL pipeline, browse endpoints, auth persistence)
- Persist queue and volume across restarts

## Changes

### Gapless Playback (`playback/gapless.py`)
- New `GaplessEngine` class for pre-buffering next track
- Detects end-of-track and starts next track decode before current finishes
- Crossfade support configurable per output type
- Integration with `Player` state machine for seamless transitions

### Playlist API (`api/routes/playlists.py`)
- Full CRUD: create, read, update, delete playlists
- Track management: add, remove, reorder tracks within playlists
- New `PlaylistRepo` in repository layer
- Pydantic request/response models for all operations

### Metadata Editing
- `PUT /library/tracks/{id}` — Update track metadata
- `PUT /library/albums/{id}` — Update album metadata
- `PUT /library/artists/{id}` — Update artist metadata
- `DELETE /library/tracks/{id}`, `DELETE /library/albums/{id}`, `DELETE /library/artists/{id}`

### Artwork Serving (`library/artwork.py`)
- `GET /library/artwork/{filename}` — Serve cached artwork images
- Artwork cache with configurable max size
- Automatic extraction from audio file tags

### Streaming Playback Unblock
- URL pipeline support: streaming tracks can be played through the audio pipeline
- Stream URL resolver with configurable timeout (`TUNE_STREAM_URL_RESOLVE_TIMEOUT`)
- Browse endpoints: `GET /streaming/{service}/albums/{id}/tracks`, artists, etc.
- Auth persistence: streaming credentials saved to `streaming_auth` table, restored on startup

### Scanner Race Condition Fix
- Fixed concurrent scan operations overwriting each other's results
- Added scan lock to prevent parallel scans
- File mtime tracking to avoid re-scanning unchanged files

### Queue & Volume Persistence
- Play queue saved to `play_queue` table on every modification
- Volume level persisted per zone in `zones` table
- Restored on zone initialization after server restart

## Technical Decisions

| Decision | Rationale |
|----------|-----------|
| Gapless via pre-buffer | Simpler than gap analysis; works across all output types |
| Playlist as separate entity (not queue alias) | Playlists are persistent, queues are ephemeral |
| Auth in DB (not filesystem) | Single source of truth, atomic with other data |
| File mtime for scan skip | Fast stat() check avoids expensive metadata re-read |

## Files Modified/Created

| File | Change |
|------|--------|
| `playback/gapless.py` | **Created** — Gapless playback engine |
| `api/routes/playlists.py` | **Created** — Playlist CRUD routes |
| `db/repository.py` | Added `PlaylistRepo` class |
| `db/schema.sql` | Added `streaming_auth` table, `file_mtime` column |
| `library/scanner.py` | Fixed race condition, added mtime check |
| `library/artwork.py` | Added artwork serving endpoint support |
| `api/routes/library.py` | Added PUT/DELETE endpoints, artwork route |
| `api/routes/streaming.py` | Added browse endpoints, auth persistence |
| `playback/player.py` | Integrated gapless engine |
| `playback/queue.py` | Added persistence hooks |
| `models.py` | Added Playlist models, edit request models |
| `config.py` | Added `STREAM_URL_RESOLVE_TIMEOUT` |

## Tests

- `test_phase2.py` — 151 integration tests covering:
  - Gapless playback transitions
  - Playlist CRUD operations
  - Track add/remove/reorder in playlists
  - Metadata editing (PUT/DELETE)
  - Artwork serving
  - Scanner mtime tracking
  - Queue persistence
  - Volume persistence
  - Streaming browse endpoints
  - Auth save/restore

## Commits

| Hash | Message |
|------|---------|
| `a5af25c` | Add phase 2 test suite: 151 new tests covering all remaining modules |
| `17d67be` | Fix AirPlay auth error causing queue to skip all tracks silently |
| `b0e805b` | Fix scanner race condition, add gapless playback, persist queue/volume |
| `57562cb` | Unblock streaming playback: URL pipeline support, stream URL resolver, browse endpoints, auth persistence |
| `b7e6a1b` | Add playlist API, metadata editing endpoints, and artwork serving |
| `4092ccf` | Replace ASCII art diagrams with Mermaid in all docs |

# Phase 4 — Playback Robustness & Queue Persistence

**Date:** 13 février 2026
**Commit:** `cf734f6`
**Tests added:** ~60

## Objectives

- Make play queue fully persistent (survives restarts)
- Support streaming tracks in the queue (source/source_id)
- Complete queue operations (move, jump, remove by position)
- Add playback error recovery with auto-skip
- Persist volume settings per zone

## Changes

### Queue Persistence
- Full queue state saved to `play_queue` table on every modification
- Current position tracked via `is_current` flag in DB
- Queue restored when zone is re-initialized after restart
- Streaming tracks stored with `source` and `source_id` for URL re-resolution

### Queue Completeness
- `DELETE /zones/{zone_id}/queue/{position}` — Remove track by position
- `PUT /zones/{zone_id}/queue/move` — Reorder track (from_position → to_position)
- `POST /zones/{zone_id}/queue/jump` — Jump to specific position
- `POST /zones/{zone_id}/queue` — Add tracks to queue (append or insert)

### Streaming Queue Support
- Queue can contain mixed local and streaming tracks
- Streaming track URLs resolved on-demand (just before playback)
- `TUNE_STREAM_URL_RESOLVE_TIMEOUT` config (default 15s)
- Failed URL resolution triggers auto-skip to next track

### Playback Error Recovery
- Pipeline errors trigger `PLAYBACK_ERROR` event then auto-advance to next track
- Configurable `TUNE_PIPELINE_START_TIMEOUT` (default 30s)
- Dead track detection: if 3 consecutive tracks fail, playback stops
- Error details included in WebSocket event for client display

### Volume Persistence
- Volume level saved to `zones` table on every change
- Restored on zone initialization
- Volume clamped to 0.0-1.0 range

## Technical Decisions

| Decision | Rationale |
|----------|-----------|
| Queue in DB (not memory) | Survives process restarts; queue can be large |
| On-demand URL resolution | Streaming URLs expire (minutes to hours); can't pre-resolve |
| 3-strike auto-stop | Prevents infinite skip loop on systemic errors |
| Position-based queue ops | Simpler API than track-ID-based (no ambiguity with duplicates) |

## Files Modified

| File | Change |
|------|--------|
| `db/repository.py` | `PlayQueueRepo`: add_tracks, set_current, count; enhanced set_queue |
| `playback/queue.py` | Persistence hooks, move/jump operations, streaming track support |
| `playback/player.py` | Error recovery, auto-skip, pipeline timeout |
| `api/routes/playback.py` | New queue endpoints (DELETE, PUT move, POST jump) |
| `models.py` | `QueueMoveRequest`, `QueueJumpRequest` models |
| `config.py` | `PIPELINE_START_TIMEOUT` setting |
| `zones/zone.py` | Volume persistence on change |
| `db/schema.sql` | `is_current` column on play_queue |

## Tests

- `test_phase4.py` — ~60 tests covering:
  - Queue save/restore across restart simulation
  - Add, remove, move, jump queue operations
  - Streaming tracks in queue with source/source_id
  - Playback error → auto-skip → next track
  - 3-strike stop behavior
  - Pipeline timeout handling
  - Volume save/restore
  - Mixed local + streaming queue

## Commit

| Hash | Message |
|------|---------|
| `cf734f6` | Add playback robustness, queue completeness, and streaming queue persistence |

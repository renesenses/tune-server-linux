# Changelog

All notable changes to Tune Server.

## v0.7.35 — 2026-04-27

### New: Spotify OAuth via mozaiklabs.fr bouncer

Spotify exige HTTPS pour les redirect URIs publics depuis 2025 (sauf
`127.0.0.1` strict loopback). Une instance Tune accédée depuis le LAN
(`http://192.168.1.x:8888`) ne pouvait plus être enregistrée chez
Spotify.

`SpotifyService.authenticate` met maintenant l'URL Tune locale dans
le param OAuth `state` (`http://<server_ip>:<port>/api/v1/streaming/
spotify/callback`) et garde `redirect_uri =
https://mozaiklabs.fr/spotify-callback`. Le bouncer
([mozaiklabs PR #2](https://github.com/renesenses/site-mozaiklabs/pull/2))
valide que le state pointe vers un host privé (loopback / RFC1918) et
302 le code vers le Tune local. spotipy préserve le state à travers
authorize → callback, donc complete_auth(code) marche sans plumbing
supplémentaire.

Setup : `TUNE_SPOTIFY_REDIRECT_URI=https://mozaiklabs.fr/spotify-callback`
+ `TUNE_SPOTIFY_CLIENT_ID=...`. Une seule redirect URI à enregistrer
dans le Spotify Developer Dashboard.

---

## v0.7.34 — 2026-04-27

### Fixes

- **`GET /api/v1/playlists/{id}/tracks` 500'd on PostgreSQL** with the
  same `Ambiguous column name 'album_title'` error v0.7.33 fixed in
  the local-search query. The `PlaylistRepository.get_tracks` SA
  select projected `tracks` (whole table, including its denormalised
  `album_title`/`artist_name`/`cover_path` columns) AND the JOINed
  `albums.title.label("album_title")` etc. — same label twice in the
  result set, asyncpg refused to map. Renamed the JOINed labels to
  `_resolved` suffix (already supported by `_row_to_track`).

User-visible: opening a transferred playlist (e.g. fip select with 66
matched tracks) now actually displays the tracks instead of an
empty 500'd view.

---

## v0.7.33 — 2026-04-27

### Fixes

- **Local-target playlist transfer 500'd on PostgreSQL** with
  `sqlalchemy.exc.InvalidRequestError: Ambiguous column name
  'album_title' in result set column descriptions`. The `tracks` table
  has a denormalised `album_title` column AND the search query also
  joined `albums` and aliased `a.title AS album_title` — asyncpg sees
  two columns with the same final label and SA refuses to map.
  Aliased every column in `_search_local_tracks` with a `track_*`
  prefix to break the ambiguity, then unwrap to the public field
  names in the dict comprehension.

End-to-end Soundiiz-style transfer is now actually working:
fip select Tidal → Local matched **66 tracks** against the local
library on .18.

---

## v0.7.32 — 2026-04-27

### Fixes (durable)

- **`SADatabase.fetchone` silently dropped INSERT...RETURNING writes**.
  The wrapper used `engine.connect()` (no transaction) for every
  fetchone, so an INSERT...RETURNING returned the new id but rolled
  back when the connection closed. Subsequent FK references then
  exploded (`ForeignKeyViolationError` on PG, silent data loss on
  SQLite). v0.7.31 worked around this in transfer.py only; v0.7.32
  fixes the root cause: fetchone/fetchall now auto-detect mutating
  statements (INSERT/UPDATE/DELETE/MERGE/REPLACE) and run them under
  `engine.begin()` so they actually commit.
- **Regression tests** (`test_is_write_statement_classifies_correctly`,
  `test_fetchone_insert_returning_persists`) now exercise the
  detection helper and the round-trip on both SQLite and PG (CI
  dual-engine matrix).
- transfer.py reverted to the natural `db.fetchone(INSERT...RETURNING)`
  form — the SA wrapper does the right thing now.

---

## v0.7.31 — 2026-04-27

### Fixes (transfer to local target)

- **Local target transfers created an empty playlist**. The transfer
  pipeline created a row in `playlists` but never wrote any
  `playlist_tracks` — the original code carried a TODO comment
  ("track insertion would need source_id → local track mapping") that
  was never implemented. Now insert matched/approximate tracks at the
  end of the create-playlist block, in match order.
- **PostgreSQL installs would have failed silently** on the same path
  because the helper used SQLite's `last_insert_rowid()` to recover
  the new playlist id. Replaced with portable `INSERT ... RETURNING
  id` (works on SQLite ≥3.35 + PostgreSQL).

---

## v0.7.30 — 2026-04-27

### Fixes (playlist transfer pipeline)

Three blockers preventing any playlist transfer from a streaming
service from completing — caught while testing the new web UI
PlaylistsHub wizard against `Deezer → Local`.

- **Method name mismatch**: route called `getPlaylistTracks()` /
  `getUserPlaylists()` (camelCase) but every streaming service exposes
  `get_playlist_tracks()` / `get_user_playlists()` (snake_case).
  AttributeError on every transfer with a streaming source.
- **Pydantic Track vs dict**: same handler then treated each track as
  a dict (`.get("title")`) but services return Pydantic Track models —
  attribute access only.
- **target_id type mismatch**: `TrackMatchResult.target_id: str` but
  the local search returns the integer DB row id, triggering
  ResponseValidationError on the response. Coerce to `str()` at the
  transfer-builder seam.

This is the Soundiiz-style cross-service transfer working end-to-end
for the first time on the Hub UI.

---

## v0.7.29 — 2026-04-27

### Fixes

- **`/api/v1/playlist-manager/services` always reported every streaming
  service as `authenticated: false`**, blocking the upcoming playlist
  transfer wizard from offering Tidal/Qobuz/YouTube/Spotify/Deezer as
  destinations even when the user was authenticated. Root cause: the
  endpoint treated `streaming_manager.status[name]` as a nested dict
  (`status.get("authenticated")`) when it is actually a plain bool.
  Always fell through to the False default.

---

## v0.7.28 — 2026-04-27

### Fixes

- **CI test suite was being SIGTERM-killed** every run since v0.7.24
  because `tests/test_updater.py::test_auto_install_skipped_on_windows`
  was written for the old behaviour where Windows updates were a
  no-op. After v0.7.24 made Windows updates work via stage-and-swap,
  the test exercised the real `_auto_install_and_restart` Windows
  branch — which calls `os.kill(os.getpid(), SIGTERM)` — without
  mocking it, and pytest itself was the process that got the signal.
  Test rewritten to assert the new behaviour (download + helper spawn
  + SIGTERM via mocks).

---

## v0.7.27 — 2026-04-27

### Fixes

- **Spotify Connect relay shutdown was 60s slow** because aiohttp
  AppRunner cleanup waits the default `shutdown_timeout` (60s) for
  active stream handlers to finish — librespot connections are
  effectively endless. Set `shutdown_timeout=2.0` and signal EOF to
  subscribers before runner cleanup so handlers exit promptly. The
  unit test suite went from 60s → 0.2s; CI tests stopped timing out.

---

## v0.7.26 — 2026-04-27

### Fixes

- **`/api/v1/system/diagnostics` reported "name 'os' is not defined"**
  for the `db` block on SQLite installs. Added missing module-level
  `import os` (the function-local imports elsewhere did not propagate
  to `_db_diagnostics`). Caught in Jacques' v0.7.24 diagnostic bundle.

---

## v0.7.25 — 2026-04-27

### Fixes

- **Windows launcher silently failed to start** on some installs (most
  visible on French Windows) because `scripts/start-tune-server.bat`
  shipped with LF-only line endings. Some `cmd.exe` builds refuse to
  execute LF batch files — the window flashes and closes with no
  output. Added `.gitattributes` enforcing `eol=crlf` for `*.bat /
  *.cmd / *.ps1`, normalised the file in-place. Reported by Jacques
  on v0.7.24.

---

## v0.7.24 — 2026-04-27

### Windows robustness pack

Targeted at the upcoming wave of Windows testers from the on-mag.fr
article — make first-run, support, and updates self-service.

- **Pre-flight launcher**: `start-tune-server.bat` now checks for
  `tune-server.exe`, `ffmpeg.exe`, and a free port 8888 before launch;
  opens the browser automatically once `/api/v1/system/health`
  responds; on exit, dumps the last 30 lines of `tune-server.log` so
  testers can paste them into a bug report.
- **File logging**: `--noconsole` previously sent stdout to devnull on
  Windows, leaving zero forensic trail. We now tee stdout/stderr to a
  rotating `tune-server.log` (next to the binary, capped at 2 MB +
  one rolled-over copy).
- **One-click diagnostics**: new `GET /api/v1/system/diagnostics/bundle`
  endpoint returns a ZIP with `diagnostics.json`, `tune-server.log`,
  and a credentials-masked `.env` copy. Web UI exposes a "Télécharger
  le diagnostic" button in Settings → About.
- **Auto-update stage-and-swap**: Windows could not update before
  because the running `tune-server.exe` is file-locked. New flow:
  download → extract to `_update_staging/` → write
  `_apply_update.bat` → spawn detached → SIGTERM ourselves → applier
  `taskkill`s, `robocopy /MIR`s, restarts the launcher. The
  `_update_staging` folder is also the signal the watchdog uses to
  step aside instead of restarting.
- **Watchdog auto-restart**: the launcher now wraps
  `tune-server.exe` in a 3-attempt retry loop with linear backoff
  (5/10/15 s). Clean exits (code 0, user closed the window, update
  hand-off) end the watchdog; only genuine crashes trigger a restart.

---

## v0.7.23 — 2026-04-27

### Fixes

- **Deezer full-track playback now produces audible audio**. v0.7.22
  fixed the format-name request bug so Deezer started returning the
  encrypted full-track URL, but those streams are Blowfish-CBC-stripe
  encrypted — DLNA renderers can't decrypt them, so playback was 30
  seconds of *silence* instead of 30 seconds of preview.

  We now ship a small decrypting HTTP proxy on the existing audio
  streamer port (8080). `get_stream_url` returns
  `http://<server>:8080/deezer/<sng_id>.flac`; the proxy fetches the
  upstream Deezer URL, derives the per-track Blowfish key from the
  SNG_ID, decrypts the stripe pattern (every 1st of every 3 2048-byte
  chunks), and pipes plain FLAC/MP3 to the renderer. Geo-restricted
  tracks transparently follow the `FALLBACK.SNG_ID` provided by
  Deezer's gateway.

### New module

- `tune_server.streaming.deezer_decrypt` — Blowfish key derivation +
  CBC-stripe chunk decryption helpers.
- `tune_server.streaming.deezer_proxy` — aiohttp route handler
  (`GET /deezer/{sng_id}.{ext}`) registered on the streamer's app.

---

## v0.7.22 — 2026-04-27

### Fixes

- **Deezer 30-second preview bug**: full-track playback was silently
  falling back to the 30-second preview clip on every request. Root
  cause: the request to `media.deezer.com/v1/get_url` was sending the
  legacy numeric format ID (`9`, `3`, `1`) instead of the format name
  string (`FLAC`, `MP3_320`, `MP3_128`) that the API now requires.
  Numeric IDs return 403 Forbidden silently and the player falls back
  to the preview URL. Confirmed against the live API across all three
  qualities.

---

## v0.7.21 — 2026-04-27

### New: Spotify Connect receiver

Tune Server now appears as a Spotify Connect device on the local network.
Open the Spotify mobile app, pick **Tune (…)** in the device list, and
playback is routed into the configured Tune zone (DLNA / AirPlay / local
soundcard). Spotify Premium is required on the *client* side; no login
or OAuth on the server (zeroconf-only auth — like a Sonos).

Configure under **Settings → Spotify Connect**: pick the target zone,
optionally set a custom device name, toggle on/off. One device ⇄ one
zone for now (multi-zone receiver is on the v0.8 roadmap).

The `librespot` binary is bundled in the official release archives
(Linux, macOS, Windows). Falls back to a `librespot` on PATH if the
bundled one is missing — typical for Homebrew (`brew install
librespot`) or APT installs.

API: `GET/POST /api/v1/spotify-connect/{status,enable,disable}`.

---

## v0.7.20 — 2026-04-27

### Fixes
- **Playback queue from remote clients**: `POST /zones/{id}/play` now
  accepts a `start_index` field on `PlayRequest`. Clients can send the
  full album as `track_ids: [...]` plus `start_index: N` so the queue
  is loaded with auto-advance and the user can navigate back. Previous
  behaviour (Flutter / iOS sending one track) made playback stop at
  end-of-track. Reported by Jacques on Android.

### A pragmatique (clients)
- Default app mode for new installs is now **remote** on every
  platform (was: server on iPad, server on Flutter). The Python server
  becomes the single source of truth for the v0.6+/v0.7.x feature set
  (Party, DJ, lyrics, EQ, album bios, recommendations…). Standalone
  stays available in Settings → Advanced with a clear warning.
- macOS combo install: `scripts/install-macos.sh` (curl-pipe-to-bash)
  installs the Python server as a launchd LaunchAgent, preserving
  user data on upgrade. Pairs with the TestFlight Tune.app for a
  100%-features local setup.

## v0.7.19 — 2026-04-27

### Stabilization
- **Auto-update opt-in** (`TUNE_AUTO_UPDATE=true`): UpdateChecker now
  downloads + installs + restarts without a UI click when the setting
  is enabled. Skipped on Windows (the running .exe is file-locked;
  stage-and-swap is deferred to v0.7.20+). Default off, enable per
  tester. Linux/macOS server users get truly silent updates when
  paired with systemd / launchd auto-restart.

## v0.7.18 — 2026-04-27

### Stabilization
- **DB backup before migrations**: SQLite databases are snapshotted to
  `tune_server.db.bak.YYYYMMDD-HHMMSS` before any ALTER runs. Keeps the
  5 most recent. Failures are non-fatal. Skipped for `:memory:` and
  PostgreSQL.
- **Enriched `/api/v1/system/diagnostics`**: now returns DB info (engine,
  path/url with masked credentials, size), live schema-drift report
  (columns in SA model not present in DB — should always be empty),
  last scan timestamp + stats, ring buffer of the last 50 warning/error
  log events, per-service streaming auth status (with auth-error if any),
  outputs health (DLNA/AirPlay device counts). Single JSON for remote
  triage — testers can `curl /api/v1/system/diagnostics` instead of
  needing journalctl access.
- **CI dual-engine** (`tests.yml`): pytest now runs on both SQLite and
  PostgreSQL via a service container. Catches PG-only / SQLite-only
  regressions before they ship.

## v0.7.17 — 2026-04-26

### Fixes
- **Critical**: schema migrations are now auto-detected from the SA model.
  v0.7.16 only fixed the transaction-scoping bug, but the explicit ALTER
  list was massively incomplete (missing `albums.bio`, all `tracks.*`
  columns added since v0.5, `artists.source_id`, `zones.muted`,
  `zones.online`, etc.). Users upgrading from older versions still got
  HTTP 500 on `/library/albums/*`, `/library/recommendations`, etc.
  The new logic compares live schema vs SA metadata and adds every
  missing column, so future column additions are auto-migrated too.

## v0.7.16 — 2026-04-26

### Fixes
- **Critical**: schema migrations on existing SQLite databases silently
  skipped after the first column-already-exists error because the entire
  batch ran in a single transaction. Each ALTER now runs in its own
  transaction. Causes `OperationalError: no such column: al.sample_rate`
  → HTTP 500 on `/library/albums/*`, `/library/recommendations`, etc.
  for users upgrading from older versions.

## v0.7.15 — 2026-04-26

### Fixes
- Dashboard / recommendations: PostgreSQL DataError "can't subtract offset-naive
  and offset-aware datetimes". Use naive UTC cutoffs to match
  `TIMESTAMP WITHOUT TIME ZONE` columns. Fix-up of v0.7.14 dashboard fix.

## v0.7.14 — 2026-04-26

### Fixes
- Dashboard / recommendations: fix 500 errors on SQLite/Windows — replaced PostgreSQL-only
  `INTERVAL` and `EXTRACT(HOUR ...)` with engine-agnostic Python cutoffs and dispatched
  hour expression. Regression introduced in v0.7.12.
- Scanner: strip NUL bytes (`\x00`) from tag values before DB insert (PostgreSQL UTF-8 rejection)
- Scanner: tolerate broken Vorbis tags raising ValueError on `tags.get()`
- Qobuz: auto-refresh credentials and retry once on 403 Forbidden (handles app_id/secret rotation)
- Watcher: fall back to polling mode when a subdirectory is unreadable (e.g. `lost+found`)
- SSDP: throttle `ssdp_device_create_error` warnings to debug after 3 consecutive failures per device

## v0.7.13 — 2026-04-26

### Features
- Alarm clock — musical wake-up with fade-in
- Collections — group albums by theme
- Quick favorites — 1-click toggle on tracks and albums
- Activity feed — recent plays across all zones
- Now listening — real-time multi-zone status
- Smart duplicate detection (local vs streaming)
- Share playlist by link (token + text)
- Room correction — per-zone audio profiles
- Import/export ratings
- Widget data endpoint for mobile apps
- Silence detection for intelligent crossfade

## v0.7.12 — 2026-04-26

### UX
- Sleep timer, Queue to Playlist, Crossfade between tracks
- Clickable dashboard stats, History tracks with play icon

### Audio
- Volume normalization (EBU R128), Crossfeed DSP, Pre-buffer gapless

### Discovery
- Recommendations, Advanced listening dashboard

### Social
- Album ratings (1-5 stars + notes), Collaborative playlists

### Diagnostics
- Restart, rescan, clear cache, view logs buttons

## v0.7.11 — 2026-04-25

### DJ Beatmatch
- BPM extraction from audio tags (TBPM/BPM/tmpo)
- BPM detection via FFmpeg + numpy autocorrelation
- Tempo sync between decks (atempo filter, 0.5-2.0x range)
- POST /dj/analyze/{track_id}, POST /dj/sync-tempo/{zone_id}
- BPM display per deck in web UI

### DJ Waveform
- Real waveform generation (800-point RMS envelope via FFmpeg + numpy)
- GET /dj/waveform/{track_id} with DB cache
- Canvas visualization with progress overlay (replaces pseudo-random)
- waveform_data + waveform_generated_at columns on tracks

### Party Mode
- Persistent votes (party_votes table, survives server restart)
- PartyVoteRepo for SQLite + PostgreSQL
- Bubble-sort reordering preserved in DB

## v0.7.10 — 2026-04-25

### DJ Mode
- Real dual-deck PCM mixer (AudioMixer + DualDeckPlayer)
- Crossfade: linear + equal-power curves, 1-30s duration
- Auto-crossfade (triggers N seconds before track end)
- Waveform visualization per deck
- Play/pause per deck, crossfader slider, gain bars

### Party Mode
- Collaborative playlist — search and add tracks
- Server-side upvote with automatic queue reordering
- QR code for sharing party link

### Now Playing
- Karaoke auto-scroll to current synced line
- Synced lyrics returned separately from lrclib.net

### Library
- Album bios (MusicBrainz → Claude AI via mozaiklabs.fr → DB cache)
- Radio favorites → streaming playlist (Tidal/Qobuz/Spotify)
- Sidebar zones: state icons + current track display

### Zones
- Auto-detect same-brand zones for zero-calibration group sync
- Calibration fixes for stopped zones

### Fixes
- Lyrics endpoint: Track model raw SQL fix
- Party route: list_zones() instead of private _zones

## v0.7.9 — 2026-04-25

### Now Playing
- Lyrics display (reads from file tags, cached in DB)
- EQ presets (Flat, Bass Boost, Treble, Vocal, Rock, Jazz, Classical) + custom bands
- Share "Now Playing" to clipboard
- Credits + Lyrics + EQ + Share buttons

### Smart Playlists
- Dynamic playlists with visual rule builder (10 fields, 6 operators, AND/OR)
- Create, edit, delete, play all
- Sort by title/artist/album/year/random

### Playback
- "Play Next" button on every track (inserts after current)
- Transfer playback between zones (queue + position + seek)
- Last.fm scrobbling (auto-scrobble on track change)

### Library
- Artist timeline (chronological discography)
- Album bio (MusicBrainz release annotations)
- Similar albums (by genre/artist)

### Party Mode
- Collaborative playlist: /party/add, /party/queue, /party/status
- Anyone on the network can search and add tracks

### Outputs
- Chromecast (Google Cast) output support
- Bluetooth already supported via Local output

### Dashboard
- Top artists and top tracks on home page with covers
- Playback history API (recent, top-tracks, top-artists)

## v0.7.8 — 2026-04-25

### Dashboard
- "Mes écoutes" on home page: recently played, top artists, top tracks with covers
- Playback history recorded automatically (played_at, duration, source)

### Now Playing
- Credits button showing musicians and instruments for current track

### Smart Playlists
- Dynamic playlists with rule builder (10 fields, 6 operators, AND/OR)
- Visual UI: create, edit, delete, play all
- Sort by title/artist/album/year/random

### Performance
- Album quality denormalized (eliminates 988M index scans per session)
- Album format/sample_rate/bit_depth refreshed on scan

### Devices
- Delete individual device button in Settings
- Clear all devices button

## v0.7.7 — 2026-04-25

### Performance
- Denormalized album quality (format/sample_rate/bit_depth stored on albums table)
- Eliminated correlated subquery on album list (was 988M index scans)
- Album quality refreshed automatically on each scan

### Playback History
- New playback_history table — records each played track automatically
- PlaybackHistoryRepo: list_recent, top_tracks, top_artists

### Fixes
- Clear devices endpoint: POST /clear (was DELETE /all — route conflict)

## v0.7.6 — 2026-04-24

### Fixes
- Fixed browse directory crash on SQLite/Windows (SPLIT_PART is PostgreSQL-only)

## v0.7.5 — 2026-04-24

### Fixes
- Fixed Windows --noconsole crash (redirect None stdout/stderr to devnull)
- Clear all devices button in Zone Manager (requested by Alban)

## v0.7.4 — 2026-04-24

### Bug Fixes
- Fixed DSF files showing 44 kHz instead of actual DSD sample rate (2.8/5.6 MHz)
- Fixed intermittent 500 errors during playback (null output check, skip race conditions)
- Fixed seek crash when output is swapped during seek operation
- Fixed SQLite BUSY errors on Windows (added 5s busy_timeout pragma)

### Search
- Multi-term search in library (e.g. "Jazz 1959" finds jazz albums from 1959)
- Search by genre, year, composer, instrument, label (in addition to title/artist)

## v0.7.3 — 2026-04-24

### Track Credits UI
- Credits button on each track in album detail view (expandable)
- Credits grouped by role (Composer, Performer, Conductor...) with clickable artist names
- Credits section on artist detail page (instruments, track count)
- MusicBrainz instrument enrichment: lookup each artist for their primary instrument
- API: POST /tracks/{id}/credits/enrich, POST /albums/{id}/credits/enrich, POST /enrich-credits

### Windows Fixes
- Fixed IP detection for DLNA streaming (ipconfig fallback for multi-NIC setups)
- AirPlay zone creation: clear error messages (Bonjour required, device not found)
- SMB/NAS discovery via `net view` command on Windows
- Zone manager: specific error messages propagated to client instead of generic 503
- mDNS: explicit warning when Bonjour is not installed
- PyInstaller: hidden console window (`--noconsole`)

## v0.7.1 — 2026-04-23

### Track Credits
- Multiple artists per track with roles and instruments
- Extract credits from PERFORMER (Vorbis/FLAC), TMCL/TIPL (ID3v2.4), COMPOSER/CONDUCTOR tags
- API: GET /tracks/{id}/credits, GET /artists/{id}/credits
- TrackCreditRepo with full CRUD

### Setup Wizards
- SMB/NAS wizard: scan network, credentials, mount, add to library (4 steps)
- Local folder wizard: path input with platform hints, add & scan (3 steps)
- Integrated in Settings with two buttons

### Artist Images
- Wikipedia disambiguation for musicians (Prince, Air, etc.)
- Search API fallback for rare artist names
- Don't skip frozen artists when image is missing

### Qobuz
- Auto-refresh app_secret via daily cron on mozaiklabs.fr
- Updated to current production secret

## v0.7.0 — 2026-04-22

### Qobuz Auto-Update Credentials
- App secret auto-refreshed from mozaiklabs.fr at startup/auth
- No more manual release needed when Qobuz changes their API secret

### Artist Metadata (AI-powered)
- Enriched artist pages: bio, anecdotes, similar artists, members, discography
- Wikipedia images (replaces Last.fm placeholders)
- Bio status: frozen for deceased/disbanded, auto-refresh for active
- Available on all platforms (web, iOS, macOS, Android)

### Bug Fixes
- Fix macOS folder picker (NSOpenPanel instead of fileImporter)
- Fix 500 error on Windows (missing Path import in sa_engine)
- Fix 500 on library (missing albums.format column migration for SA engine)
- Fix artist page wheel-of-death on macOS (10s timeout + MainActor)
- Fix relative image URLs from mozaiklabs.fr
- Fix gapless DSD passthrough (SetNextAVTransportURI)
- Fix album track ordering for box sets (file_path tiebreaker)
- Deduplicate 14 artist entries

### Documentation
- Mermaid.js diagrams on mozaiklabs.fr
- Architecture docs linked from homepage + nav menu
- Keyboard shortcuts page
- Prep documentation for press

## v0.6.9 — 2026-04-21

### Artist Metadata Enrichment
- AI-powered artist bios via mozaiklabs.fr API (MusicBrainz + Last.fm + Claude)
- Enriched artist detail page: bio, anecdotes, similar artists, members, discography
- Similar artists are clickable (navigate to artist if in library)
- Artist cover art stored locally on mozaiklabs.fr
- Bio status: frozen for deceased/disbanded artists, auto-refresh for active

### Bug Fixes
- Fix gapless playback for DSD passthrough and direct URL tracks (SetNextAVTransportURI)
- Fix album track ordering for box sets (file_path tiebreaker)
- Deduplicate 14 artist entries (case-insensitive merge)

### All Platforms
- Web client: enriched artist page with collapsible sections
- iOS/macOS: ArtistDetailView with metadata loading
- Flutter/Android: artist detail page with expandable sections, 8 languages

## v0.6.8 — 2026-04-19

### Refactoring
- Split metadata_manager.py (1900 lines) into sub-package: track_edits, batch, suggestions, covers
- Remove 10 unused table definitions from ORM (users, sessions, stats, etc.)
- Replace print() with structured logging in database migration
- Fix bpm column type (INTEGER → REAL) in SQLite schema
- Clean 11 stale web asset files from repository
- Add coverage files to .gitignore

## v0.6.7 — 2026-04-19

### Stereo Pairing
- L/R channel split across two DLNA devices via ffmpeg pan filter
- API: create/dissolve/list stereo pairs
- Stereo pairs auto-grouped with SyncEngine for tight synchronization

### Zone Manager UI
- Dedicated zone management page with grid layout
- Visual zone groups (color-coded borders) and stereo pair L/R badges
- Group/ungroup zones, create/dissolve stereo pairs from UI
- Volume sliders, latency measurement, device assignment
- Unbound devices section for quick zone creation

### Onboarding Wizard
- 4-step first-launch setup: Welcome → Music Library → Streaming → Done
- Auto-detected on first launch (empty library + no services)
- Library scan with progress, streaming service enable + auth inline

### UX Improvements
- Unified toast notification system (error/success/info, auto-dismiss)
- Diagnostics page: server health, DB stats, zones, devices, copy to clipboard
- Global API error handler (5xx + network errors → toast)

### Robustness
- AirPlay: exponential backoff on reconnect (2s/5s/10s/30s max)
- Streaming connectors: shared HTTP retry with backoff (Qobuz, Deezer, Amazon, YouTube)
- Configurable API timeout (TUNE_API_TIMEOUT, default 60s)

### CI
- Linux/macOS release archives now include install.sh, tune-server.service, README, CHANGELOG

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

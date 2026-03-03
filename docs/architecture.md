# Architecture Overview

## Design Principles

1. **Async-first** — Single asyncio event loop, no threads except where forced by libraries (sounddevice callback)
2. **Event-driven** — Loose coupling via pub/sub event bus; components communicate through events, not direct calls
3. **Output-agnostic** — All outputs implement a common `OutputTarget` protocol; the player doesn't know if it's feeding DLNA, AirPlay, or a local soundcard
4. **Bit-perfect when possible** — Never upsample; passthrough original file bytes when the output supports the source format
5. **Fail gracefully** — Network devices appear and disappear; the server adapts without crashing

## System Diagram

```mermaid
graph TD
    subgraph Process["Tune Server Process"]
        API["FastAPI REST API :8888<br>+ WebSocket"]
        BUS["Event Bus<br>(async pub/sub, 40 event types)"]
        DEPS["App Deps (DI container)"]

        API <--> BUS
        API --> DEPS

        subgraph Subscribers["Event Bus Subscribers"]
            direction LR
            WS_SUB["WebSocket Manager<br>(fnmatch filtering)"]
            ZM_SUB["Zone Manager"]
            SYNC_SUB["Sync Engine"]
            DISC_SUB["Discovery Manager"]
        end
        BUS --> Subscribers

        subgraph Core["Core Components"]
            LIB["Library Scanner"]
            ZM["Zone Manager"]
            DISC["Discovery Manager"]
            STREAMING["Streaming Services<br>(Tidal / Qobuz / YouTube / Amazon / Spotify / Deezer)"]
        end
        DEPS --> Core

        LIB --> DB["SQLite + FTS5"]
        DISC --> SSDP_MDNS["SSDP / mDNS"]

        subgraph Zones["Zone (1..N)"]
            PLAYER["Player<br>(state machine)"]
            QUEUE["Queue<br>(persistent)"]
            GAPLESS["Gapless Engine"]
            OUTPUT["Output Target"]
        end
        ZM --> Zones

        PLAYER --> PIPELINE["Audio Pipeline<br>(decode / passthrough)"]
        OUTPUT --> DLNA["DLNA Output"]
        OUTPUT --> AIRPLAY["AirPlay Output"]
        OUTPUT --> LOCAL["Local Output"]

        DLNA --> HTTP["HTTP Audio Streamer :8080<br>Range requests, DLNA headers"]
    end

    CLIENTS["REST / WS Clients"] --> API
    HTTP --> DMR["DLNA Renderers<br>(pull HTTP)"]
    AIRPLAY --> AIRDEV["AirPlay Devices"]
```

## Streaming Services

```mermaid
graph TD
    REGISTRY["Service Registry<br>(deps.streaming_services)"]

    subgraph Services["Streaming Services"]
        TIDAL["Tidal<br>tidalapi<br>OAuth device flow<br>HI_RES_LOSSLESS"]
        QOBUZ["Qobuz<br>aiohttp<br>App ID/Secret<br>up to 24/192"]
        YOUTUBE["YouTube Music<br>ytmusicapi + yt-dlp<br>OAuth file<br>best audio"]
        AMAZON["Amazon Music<br>undocumented API<br>OAuth device flow<br>SD/HD/ULTRA_HD"]
        SPOTIFY["Spotify<br>aiohttp<br>PKCE OAuth<br>OGG/AAC (Free/Premium)"]
        DEEZER["Deezer<br>aiohttp<br>OAuth<br>FLAC/MP3"]
    end

    REGISTRY --> TIDAL
    REGISTRY --> QOBUZ
    REGISTRY --> YOUTUBE
    REGISTRY --> AMAZON
    REGISTRY --> SPOTIFY
    REGISTRY --> DEEZER

    subgraph Common["Common Interface (StreamingService)"]
        AUTH["authenticate()"]
        SEARCH["search(query, limit)"]
        GETTRACK["get_track(id)"]
        GETURL["get_stream_url(id)"]
        BROWSE["get_album_tracks()<br>get_artist_albums()"]
        PERSIST["save_auth() / restore_auth()"]
    end

    Services --> Common
    Common --> CACHE["StreamUrlCache<br>(TTL per service)"]
    Common --> AUTHDB["streaming_auth table<br>(token persistence)"]
```

## Component Lifecycle

### Startup Sequence (`TuneServer.start()`)

```mermaid
flowchart TD
    A["Configure logging (structlog)"] --> B["Check FFmpeg availability"]
    B --> C["Connect database (SQLite WAL mode)"]
    C --> D["Initialize repositories<br>(Track, Album, Artist, Queue, Zone, Playlist)"]
    D --> E["Create LibraryScanner"]
    E --> F["Create ZoneManager<br>+ register output factories"]
    F --> G["Start DiscoveryManager (SSDP + mDNS)"]
    G --> H["Wait 2s for initial device discovery"]
    H --> I["Initialize zones from DB<br>(restore persisted zones)"]
    I --> I2["Clean up stale zones<br>(unavailable devices)"]
    I2 --> J["Setup streaming services<br>(Tidal/Qobuz/YouTube/Amazon/Spotify/Deezer if enabled)"]
    J --> J2["Restore streaming auth from DB"]
    J2 --> K["Populate API dependency container"]
    K --> L["Start WebSocket manager<br>(heartbeat interval)"]
    L --> M["Start SyncEngine"]
    M --> N["Start FileSystemWatcher (if enabled)"]
    N --> O["Start MetadataEnricher"]
    O --> P["Trigger initial library scan (if enabled)"]
    P --> Q["Emit SYSTEM_STARTED event"]
    Q --> R["Start Uvicorn (FastAPI)"]
```

### Shutdown Sequence (`TuneServer.stop()`)

Each component is stopped with `_safe_stop(coro, name, timeout)` to prevent one hung component from blocking the entire shutdown.

```mermaid
flowchart TD
    A["Emit SYSTEM_STOPPING event"] --> B["_safe_stop: MetadataEnricher"]
    B --> C["_safe_stop: FileSystemWatcher"]
    C --> D["_safe_stop: SyncEngine"]
    D --> E["_safe_stop: WebSocket manager"]
    E --> F["_safe_stop: DiscoveryManager"]
    F --> G["_safe_stop: All zones<br>(stop playback, close outputs, persist state)"]
    G --> H["_safe_stop: HTTP Audio Streamer"]
    H --> I["_safe_stop: Streaming services<br>(save auth, close HTTP sessions)"]
    I --> J["Close database"]
```

## Request Flow Examples

### Play a Track on DLNA

```mermaid
sequenceDiagram
    participant C as Client
    participant API as API
    participant ZM as ZoneManager
    participant P as Player
    participant PL as Pipeline
    participant DO as DlnaOutput
    participant HS as HttpStreamer
    participant DMR as DLNA Renderer

    C->>API: POST /zones/4/play {track_id: 42}
    API->>ZM: get_zone(4)
    ZM->>P: zone.player.play()
    P->>PL: pipeline.start()
    Note over PL: can_passthrough? YES (AAC→DLNA)
    P->>DO: output.start()
    DO->>HS: create_session()
    HS-->>DO: stream_url
    DO->>DMR: SetTransportURI(stream_url)
    DO->>DMR: Play()
    DMR->>HS: GET /stream/...
    HS-->>DMR: 200 + file bytes
    Note over DMR: Audio plays
    API-->>C: 200 OK
```

### Play a Track on Local Output

```mermaid
flowchart LR
    C[Client] --> API --> P[Player] --> PL[Pipeline]
    PL -->|passthrough? NO| DEC[FFmpeg Decoder<br>async subprocess]
    DEC -->|raw PCM chunks| BUF[Output Buffer]
    BUF --> SD[sounddevice<br>callback]
    SD --> SPK["Speakers"]
```

### Federated Search

```mermaid
sequenceDiagram
    participant C as Client
    participant API as API
    participant LOCAL as Local FTS5
    participant TIDAL as Tidal
    participant YT as YouTube Music
    participant AMZN as Amazon Music
    participant SPOT as Spotify
    participant DEEZ as Deezer

    C->>API: GET /search?q=radiohead
    par Parallel queries
        API->>LOCAL: full_text_search("radiohead")
        API->>TIDAL: search("radiohead")
        API->>YT: search("radiohead")
        API->>AMZN: search("radiohead")
        API->>SPOT: search("radiohead")
        API->>DEEZ: search("radiohead")
    end
    LOCAL-->>API: SearchResult
    TIDAL-->>API: SearchResult
    YT-->>API: SearchResult
    AMZN--xAPI: Error (logged, ignored)
    SPOT-->>API: SearchResult
    DEEZ-->>API: SearchResult
    API-->>C: FederatedSearchResult
    Note over C: local + tidal + youtube + spotify + deezer results<br>(amazon omitted due to error)
```

## Thread Model

```mermaid
graph TD
    subgraph MAIN["Main Thread (asyncio event loop)"]
        FASTAPI["FastAPI / Uvicorn (ASGI)"]
        AIOHTTP["aiohttp (HTTP Streamer)"]
        EVBUS["Event Bus dispatch"]
        DISCOVERY["SSDP / mDNS discovery tasks"]
        APIPE["Audio pipeline<br>(decoder reads, buffer mgmt)"]
        PLOOP["Playback loop<br>(feed chunks to output)"]
        SENG["Sync engine polling"]
        FSW["Filesystem watcher"]
        META["Metadata enricher"]
    end

    subgraph SD_THREAD["sounddevice callback thread"]
        SD["Reads from output buffer<br>writes to audio hardware"]
    end

    subgraph FFMPEG["FFmpeg subprocesses"]
        FF["Spawned per decode operation<br>(stdin/stdout pipes)"]
    end

    APIPE -.->|"async read"| FF
    PLOOP -.->|"buffer"| SD
```

## Network Discovery & Shares

```mermaid
graph TD
    subgraph DM["Discovery Manager"]
        SSDP["SSDP Discovery<br>(30s cycle)"]
        MDNS["mDNS Discovery<br>(10s cycle)"]
        NS["Network Share Discovery<br>(SMB/NFS via mDNS)"]
        MS["Media Server Discovery<br>(DLNA MediaServers)"]
        REG["Unified Device Registry"]

        SSDP --> REG
        MDNS --> REG
    end

    subgraph Network["Network Services"]
        SMB["SMB Shares"]
        NFS["NFS Exports"]
        DLNA_MS["DLNA MediaServers"]
    end

    NS --> SMB
    NS --> NFS
    MS --> DLNA_MS

    subgraph Mounts["Mount Manager"]
        MOUNT["mount/umount<br>(sudo on Linux)"]
        PERSIST["Mount persistence<br>(SQLite)"]
    end

    SMB --> MOUNT
    NFS --> MOUNT
```

Network shares discovered via mDNS can be mounted to local directories and added to `TUNE_MUSIC_DIRS` for library scanning. The mount manager handles `mount`/`umount` system calls (via `sudo` on Linux, with sudoers configuration).

DLNA MediaServers are browsable via the `/network/media-servers` API — their ContentDirectory can be navigated and individual items can be streamed.

## Metadata Management

The library supports full metadata editing and artwork management:

- **Metadata editing**: PUT endpoints for tracks, albums, and artists update fields in SQLite
- **Tag writing**: changes to title, artist, and album are written back to audio files via mutagen (FLAC, MP3/ID3, M4A/MP4, OGG Vorbis). Runs in a thread pool (`asyncio.to_thread`) to avoid blocking the event loop
- **Artwork pipeline**: embedded art extraction → MusicBrainz fallback → manual upload
- **Duplicate detection**: album merge-duplicates endpoint identifies same title+artist albums and consolidates tracks
- **Completeness stats**: tracks albums/artists missing cover art, genre, year, or images

## DLNA Direct URL Passthrough

For streaming service tracks (Qobuz, Tidal) played on DLNA renderers, the server can bypass the local audio pipeline entirely:

```mermaid
sequenceDiagram
    participant P as Player
    participant DO as DlnaOutput
    participant DMR as DLNA Renderer
    participant CDN as Streaming CDN

    P->>DO: start(stream_info, track)
    Note over DO: track.file_path is HTTPS URL<br>format is FLAC → direct passthrough
    DO->>DMR: SetTransportURI(CDN URL, DIDL-Lite)
    DO->>DMR: Play()
    DMR->>CDN: GET (FLAC stream)
    CDN-->>DMR: FLAC audio data
    Note over DMR: Native FLAC decoding<br>bit-perfect playback
```

This avoids the decode→PCM→WAV pipeline that can cause audio artifacts with 24-bit content, since WAV PCM format code 1 is only specified for up to 16-bit audio.

Supported direct formats: FLAC, MP3, AAC. Falls back to the standard pipeline for local files, unsupported formats, or non-DLNA outputs.

## Web Client

The server can serve a built Svelte SPA (Single Page Application) when `TUNE_WEB_DIR` is configured:

- Static assets with Vite content-hashed filenames are served with long cache headers
- All non-API, non-asset routes fall back to `index.html` (SPA routing)
- Both API and UI are served from the same `:8888` port
- The web client connects via WebSocket for real-time playback state updates

Build and embed: `TUNE_WEB_DIR=/path/to/tune-web-client/dist python -m tune_server`

## Error Handling Strategy

| Scenario | Behavior |
|----------|----------|
| DLNA device disappears mid-playback | HTTP connection drops, stream session cleaned up, zone remains (device marked unavailable) |
| FFmpeg decode error | Pipeline stops, player emits PLAYBACK_ERROR, auto-skips to next track |
| Database error | Logged, operation fails gracefully, server continues |
| Network discovery fails | Logged, retried on next scan cycle (30s for SSDP, 10s for mDNS) |
| Zone creation with unavailable device | Factory waits up to 15s for discovery, then returns 503 |
| Server restart with stale zones | Zones with unavailable outputs are cleaned from DB |
| Client disconnects from HTTP stream | `ClientConnectionResetError` caught silently |
| Streaming service error during federated search | Logged as warning, results from other services still returned |
| Streaming URL resolution timeout | PLAYBACK_ERROR emitted, auto-skip to next track |
| 3 consecutive playback failures | Playback stops entirely (prevents infinite skip loop) |
| AirPlay auth error | Retry with backoff, zone marked degraded if persistent |
| WebSocket client disconnect during broadcast | Connection removed from set, broadcast continues to other clients |

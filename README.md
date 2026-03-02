# Tune Server

A multi-room music server for local libraries and streaming services, with DLNA/UPnP, AirPlay, and local audio output. Designed for **Debian/Ubuntu Linux**.

## Features

- **Library Management** — Scan local music folders, extract metadata (mutagen), full-text search (FTS5), browse by directory
- **Metadata Editing** — Edit track/album/artist metadata, upload artwork, MusicBrainz enrichment, duplicate album merging
- **Multiple Outputs** — DLNA/UPnP renderers, AirPlay devices, local soundcard
- **Multi-Room** — Group zones for synchronized playback
- **Streaming Services** — Tidal, Qobuz, YouTube Music, Amazon Music, Spotify, and Deezer integration with featured content browsing
- **Federated Search** — Search across local library and all streaming services simultaneously
- **Playlists** — Full CRUD with track management and real-time sync events
- **Internet Radio** — CRUD management of Icecast/Shoutcast stations, M3U/PLS import, cover upload, genre filtering and favorites
- **Bit-Perfect Playback** — Passthrough when the output supports the source format; direct URL passthrough for DLNA + streaming services
- **Native DSD** — DSF/DFF bit-perfect passthrough to DSD-capable DLNA renderers (auto-detected via GetProtocolInfo or device heuristic); PCM transcoding fallback (176.4kHz/24-bit) for non-DSD devices
- **Gapless Playback** — Seamless track transitions with pre-buffering
- **Device Discovery** — Automatic SSDP (DLNA) and mDNS (AirPlay) scanning
- **Network Shares** — Discover, mount, and scan SMB/NFS network shares; browse DLNA MediaServers
- **Web Client** — Embedded Svelte SPA served from the same port as the API
- **Real-Time Events** — WebSocket push with subscribe/unsubscribe filtering (fnmatch patterns)
- **Background Enrichment** — MusicBrainz metadata and artwork lookup
- **Security** — Optional API key authentication, configurable CORS origins

## Architecture

```mermaid
graph TD
    subgraph Clients["Clients"]
        WEB["Web UI (Svelte 5)"]
        CLI["curl / scripts"]
    end

    subgraph Server["Tune Server Process"]
        API["REST API :8888<br>(106 endpoints)<br>+ WebSocket"]
        BUS["Event Bus<br>(40 event types)"]

        subgraph Core
            LIB["Library<br>Scanner"]
            ZONE["Zone<br>Manager"]
            AUDIO["Audio<br>Pipeline"]
            DISC["Discovery<br>SSDP / mDNS"]
        end

        subgraph Streaming["Streaming Services"]
            TIDAL["Tidal<br>(HiRes FLAC)"]
            QOBUZ["Qobuz<br>(HiRes FLAC)"]
            YT["YouTube Music<br>(yt-dlp)"]
            AMZN["Amazon Music<br>(HD/Ultra HD)"]
            SPOT["Spotify<br>(previews)"]
            DEEZ["Deezer<br>(previews)"]
        end
    end

    subgraph Storage
        DB[("SQLite<br>FTS5")]
        FS[("Music Files<br>+ SMB/NFS")]
    end

    subgraph Outputs["Audio Outputs"]
        DLNA["DLNA/UPnP<br>Renderers"]
        AIRPLAY["AirPlay<br>(pyatv)"]
        LOCAL["Local<br>(sounddevice)"]
        HTTP["HTTP Streamer<br>:8080"]
    end

    WEB & CLI --> API
    API <--> BUS
    BUS --- Core
    BUS --- Streaming
    LIB --- DB & FS
    AUDIO --- FFMPEG["FFmpeg"]
    ZONE --> DLNA & AIRPLAY & LOCAL
    DLNA --> HTTP
```

## Requirements

- **OS**: Debian 12+ / Ubuntu 22.04+
- **Python**: 3.11+
- **FFmpeg**: for audio decoding/transcoding

## Installation

### Option 1: Debian package (recommended for production)

Build and install the `.deb` package, which handles everything: dependencies, systemd service, user creation, web UI.

```bash
# On the build machine
git clone git@github.com:renesenses/tune-server.git
cd tune-server

# Build the web client (requires Node.js)
cd /path/to/tune-web-client
npm ci && npm run build
cp -r dist/ /path/to/tune-server/web/

# Build the .deb package (requires debhelper)
sudo apt install build-essential debhelper python3 python3-venv python3-pip
cd /path/to/tune-server
./build-deb.sh

# Install on the target machine
sudo dpkg -i ../tune-server_*.deb
sudo apt install -f  # install any missing dependencies
```

After installation:
1. Edit `/opt/tune-server/.env` to configure music directories and streaming services
2. Start the service: `sudo systemctl start tune-server`
3. Open `http://<server-ip>:8888` in your browser

### Option 2: Quick install script

```bash
git clone git@github.com:renesenses/tune-server.git
cd tune-server
sudo ./install.sh           # Install to /opt/tune-server
sudo ./install.sh --systemd # Install + enable systemd service
```

### Option 3: Development setup

```bash
# System dependencies
sudo apt update
sudo apt install -y python3 python3-pip python3-venv ffmpeg \
    libasound2-dev libportaudio2 portaudio19-dev \
    avahi-daemon

# Clone and install
git clone git@github.com:renesenses/tune-server.git
cd tune-server
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"

# Run
cp .env.example .env  # edit as needed
python -m tune_server
```

### Option 4: Docker

```bash
docker build -t tune-server .
docker run -d --name tune-server \
    --network host \
    -v /path/to/music:/music:ro \
    -v tune-data:/data \
    tune-server
```

`--network host` is required for DLNA/SSDP multicast discovery and mDNS.

### Upgrading

**Debian package:**
```bash
sudo dpkg -i tune-server_<new-version>.deb
# .env is preserved (conffile), service restarts automatically
```

**Manual install:**
```bash
cd /path/to/tune-server
git pull
source .venv/bin/activate
pip install -e .
sudo systemctl restart tune-server
```

## Configuration

All settings use environment variables with the `TUNE_` prefix. Copy `.env.example` to `.env` to get started:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|----------|---------|-------------|
| **Library** | | |
| `TUNE_MUSIC_DIRS` | `["~/Music"]` | Directories to scan for music (JSON array) |
| `TUNE_DB_PATH` | `tune_server.db` | SQLite database path |
| `TUNE_SCAN_ON_STARTUP` | `true` | Scan library on startup |
| `TUNE_WATCH_FILESYSTEM` | `true` | Watch for file changes |
| `TUNE_ARTWORK_CACHE_DIR` | `artwork_cache` | Directory for cached album artwork |
| **Server** | | |
| `TUNE_API_HOST` | `0.0.0.0` | API listen address |
| `TUNE_API_PORT` | `8888` | API port |
| `TUNE_STREAM_HOST` | `0.0.0.0` | Audio stream server address |
| `TUNE_STREAM_PORT` | `8080` | Audio stream server port |
| `TUNE_LOG_LEVEL` | `INFO` | Log level |
| `TUNE_LOG_FORMAT` | `console` | `console` or `json` |
| **Security** | | |
| `TUNE_API_KEY` | `None` | API key for authentication (None = no auth) |
| `TUNE_CORS_ORIGINS` | `["*"]` | Allowed CORS origins |
| **Web UI** | | |
| `TUNE_WEB_DIR` | `None` | Path to built SPA (enables embedded web UI) |
| **Streaming** | | |
| `TUNE_TIDAL_ENABLED` | `false` | Enable Tidal integration |
| `TUNE_TIDAL_QUALITY` | `HI_RES_LOSSLESS` | Tidal quality: `LOW`, `HIGH`, `LOSSLESS`, `HI_RES_LOSSLESS` |
| `TUNE_QOBUZ_ENABLED` | `false` | Enable Qobuz integration |
| `TUNE_QOBUZ_APP_ID` | `None` | Qobuz application ID |
| `TUNE_QOBUZ_APP_SECRET` | `None` | Qobuz application secret |
| `TUNE_YOUTUBE_ENABLED` | `false` | Enable YouTube Music integration |
| `TUNE_YOUTUBE_CLIENT_ID` | `None` | Google OAuth client ID (TVs and Limited Input devices) |
| `TUNE_YOUTUBE_CLIENT_SECRET` | `None` | Google OAuth client secret |
| `TUNE_AMAZON_MUSIC_ENABLED` | `false` | Enable Amazon Music integration |
| `TUNE_AMAZON_MUSIC_REGION` | `us` | Amazon Music region |
| `TUNE_AMAZON_MUSIC_QUALITY` | `HD` | Amazon quality: `SD`, `HD`, `ULTRA_HD` |
| `TUNE_SPOTIFY_ENABLED` | `false` | Enable Spotify integration |
| `TUNE_SPOTIFY_CLIENT_ID` | `None` | Spotify app client ID |
| `TUNE_SPOTIFY_REDIRECT_URI` | `http://localhost:8888/api/v1/streaming/spotify/callback` | Spotify OAuth redirect URI |
| `TUNE_DEEZER_ENABLED` | `false` | Enable Deezer integration |
| `TUNE_DEEZER_APP_ID` | `None` | Deezer app ID |
| `TUNE_DEEZER_APP_SECRET` | `None` | Deezer app secret |
| `TUNE_DEEZER_REDIRECT_URI` | `http://localhost:8888/api/v1/streaming/deezer/callback` | Deezer OAuth redirect URI |
| **Discovery** | | |
| `TUNE_DISCOVERY_ENABLED` | `true` | Enable network device discovery |
| `TUNE_SSDP_ENABLED` | `true` | Enable SSDP (DLNA renderer discovery) |
| `TUNE_MDNS_ENABLED` | `true` | Enable mDNS (AirPlay discovery) |
| **Network** | | |
| `TUNE_NETWORK_SHARES_ENABLED` | `false` | Enable SMB/NFS share discovery |
| `TUNE_NETWORK_MEDIA_SERVERS_ENABLED` | `false` | Enable DLNA MediaServer discovery |
| `TUNE_SMB_MOUNT_DIR` | `~/.tune/mounts` | Directory for network share mount points |
| **WebSocket** | | |
| `TUNE_WS_HEARTBEAT_INTERVAL` | `30` | WebSocket ping interval (seconds, 0 = disabled) |

## Usage

```bash
# Start the server
python -m tune_server

# Or via the entry point
tune-server
```

The server starts on two ports:
- **:8888** — REST API + WebSocket (+ Web UI if configured)
- **:8080** — HTTP audio streaming (for DLNA renderers)

### With embedded Web UI

Build the web client and point `TUNE_WEB_DIR` to the output:

```bash
# Build the web client
cd /path/to/tune-web-client
npm ci && npm run build

# Start the server with the embedded UI
TUNE_WEB_DIR=/path/to/tune-web-client/dist python -m tune_server
```

Open `http://localhost:8888` in your browser — both API and UI are served from the same port.

## systemd

A systemd unit file is provided for running Tune Server as a system service:

```bash
sudo cp tune-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tune-server
sudo journalctl -u tune-server -f
```

## API

### Library

```bash
# Search
curl "localhost:8888/api/v1/library/search?q=bashung"

# Browse
curl localhost:8888/api/v1/library/tracks
curl localhost:8888/api/v1/library/albums
curl localhost:8888/api/v1/library/artists
curl "localhost:8888/api/v1/library/albums/236/tracks"

# Trigger scan
curl -X POST localhost:8888/api/v1/system/scan
```

### Federated Search

```bash
# Search across local library + all streaming services
curl "localhost:8888/api/v1/search?q=radiohead&limit=10"

# Search specific sources only
curl "localhost:8888/api/v1/search?q=radiohead&sources=local,tidal,youtube"
```

### Playlists

```bash
# Create a playlist
curl -X POST localhost:8888/api/v1/playlists \
  -H 'Content-Type: application/json' \
  -d '{"name": "Favorites", "description": "My top tracks"}'

# List playlists
curl localhost:8888/api/v1/playlists

# Add tracks to a playlist
curl -X POST localhost:8888/api/v1/playlists/1/tracks \
  -H 'Content-Type: application/json' \
  -d '{"track_ids": [42, 43, 44]}'

# Get playlist tracks
curl localhost:8888/api/v1/playlists/1/tracks
```

### Devices

```bash
# List discovered network devices (DLNA/AirPlay)
curl localhost:8888/api/v1/devices

# List local audio output devices (USB DACs, soundcards)
curl localhost:8888/api/v1/devices/audio
```

### Zones

```bash
# List zones
curl localhost:8888/api/v1/zones

# Create a DLNA zone
curl -X POST localhost:8888/api/v1/zones \
  -H 'Content-Type: application/json' \
  -d '{"name": "Living Room", "output_type": "dlna", "output_device_id": "<device-id>"}'

# Create a local zone
curl -X POST localhost:8888/api/v1/zones \
  -H 'Content-Type: application/json' \
  -d '{"name": "Local", "output_type": "local"}'

# Delete a zone
curl -X DELETE localhost:8888/api/v1/zones/2
```

### Playback

```bash
# Play a track
curl -X POST localhost:8888/api/v1/zones/1/play \
  -H 'Content-Type: application/json' \
  -d '{"track_id": 42}'

# Play an album
curl -X POST localhost:8888/api/v1/zones/1/play \
  -H 'Content-Type: application/json' \
  -d '{"album_id": 10}'

# Pause / Resume / Stop
curl -X POST localhost:8888/api/v1/zones/1/pause
curl -X POST localhost:8888/api/v1/zones/1/resume
curl -X POST localhost:8888/api/v1/zones/1/stop

# Next / Previous
curl -X POST localhost:8888/api/v1/zones/1/next
curl -X POST localhost:8888/api/v1/zones/1/previous

# Seek (ms)
curl -X POST localhost:8888/api/v1/zones/1/seek \
  -H 'Content-Type: application/json' \
  -d '{"position_ms": 60000}'

# Volume (0.0 - 1.0)
curl -X POST localhost:8888/api/v1/zones/1/volume \
  -H 'Content-Type: application/json' \
  -d '{"volume": 0.7}'

# Queue
curl localhost:8888/api/v1/zones/1/queue
```

### Streaming Services

```mermaid
graph LR
    subgraph Full["Full Streaming (HiRes)"]
        TIDAL["Tidal<br>OAuth Device Code"]
        QOBUZ["Qobuz<br>Email / Password"]
        YT["YouTube Music<br>Google OAuth Device Code"]
        AMZN["Amazon Music<br>OAuth Device Code"]
    end

    subgraph Preview["Navigation + Previews (30s)"]
        SPOT["Spotify<br>OAuth PKCE"]
        DEEZ["Deezer<br>OAuth 2.0"]
    end

    TIDAL & QOBUZ & YT & AMZN --> PLAY["Stream complet<br>FLAC / HiRes"]
    SPOT & DEEZ --> PREV["Preview MP3<br>30 secondes"]
```

```bash
# List available services with auth status
curl localhost:8888/api/v1/streaming/services

# Authenticate a service
curl -X POST localhost:8888/api/v1/streaming/tidal/auth

# Search a service
curl "localhost:8888/api/v1/streaming/youtube/search?q=radiohead&limit=10"

# Browse service catalog
curl localhost:8888/api/v1/streaming/tidal/albums/12345/tracks

# Featured content
curl localhost:8888/api/v1/streaming/qobuz/featured/sections
curl "localhost:8888/api/v1/streaming/qobuz/featured/new-releases?limit=20"

# Disconnect a service
curl -X POST localhost:8888/api/v1/streaming/tidal/disconnect
```

### Network

```bash
# Discover network shares (SMB/NFS)
curl localhost:8888/api/v1/network/shares

# Scan a specific host
curl "localhost:8888/api/v1/network/scan-host?host=192.168.1.10&protocol=smb"

# Mount a share
curl -X POST localhost:8888/api/v1/network/mounts \
  -H 'Content-Type: application/json' \
  -d '{"host": "192.168.1.10", "share": "music", "protocol": "smb"}'

# Browse DLNA media servers
curl localhost:8888/api/v1/network/media-servers
curl "localhost:8888/api/v1/network/media-servers/<server-id>/browse?object_id=0"
```

### Library Management

```bash
# Browse by directory
curl localhost:8888/api/v1/library/browse
curl "localhost:8888/api/v1/library/browse/dir?path=/home/user/Music/Rock"

# Metadata completeness stats
curl localhost:8888/api/v1/library/stats/completeness

# Upload album artwork
curl -X POST localhost:8888/api/v1/library/albums/236/artwork \
  -F "file=@cover.jpg"

# Rescan artwork for all albums without cover
curl -X POST localhost:8888/api/v1/library/artwork/rescan

# Merge duplicate albums
curl -X POST localhost:8888/api/v1/library/albums/merge-duplicates
```

### Radios

```bash
# List radios
curl localhost:8888/api/v1/radios

# Create a radio station
curl -X POST localhost:8888/api/v1/radios \
  -H 'Content-Type: application/json' \
  -d '{"name": "FIP", "stream_url": "https://icecast.radiofrance.fr/fip-hifi.aac", "genre": "Éclectique"}'

# Upload radio cover
curl -X POST localhost:8888/api/v1/radios/1/artwork -F "file=@logo.jpg"

# Import from M3U/PLS
curl -X POST localhost:8888/api/v1/radios/import -F "file=@stations.m3u"

# Play on a zone
curl -X POST localhost:8888/api/v1/radios/1/play/1
```

### Multi-Room

```bash
# Group zones (zone 4 = leader, zone 1 = follower)
curl -X POST localhost:8888/api/v1/zones/group \
  -H 'Content-Type: application/json' \
  -d '{"leader_id": 4, "zone_ids": [1, 4]}'

# List groups
curl localhost:8888/api/v1/zones/groups/list

# Dissolve a group
curl -X DELETE localhost:8888/api/v1/zones/group/<group-id>
```

When zones are grouped, play/pause/stop/next/previous commands on any zone in the group affect all zones.

### WebSocket

Connect to `ws://localhost:8888/ws` for real-time events:

```json
{"type": "playback.started", "data": {"zone_id": 1, "track_id": 42}, "source": "player"}
{"type": "playlist.created", "data": {"id": 1, "name": "Favorites"}, "source": "api"}
{"type": "device.discovered", "data": {"id": "...", "name": "DMP-A8", "type": "dlna"}, "source": "ssdp"}
```

**Subscribe to specific events (fnmatch patterns):**

```json
{"action": "subscribe", "patterns": ["playback.*", "playlist.*"]}
```

**Unsubscribe (reset to all events):**

```json
{"action": "unsubscribe", "patterns": []}
```

### System

```bash
# Health check
curl localhost:8888/api/v1/system/health

# System stats
curl localhost:8888/api/v1/system/stats

# Server configuration
curl localhost:8888/api/v1/system/config

# Scan status
curl localhost:8888/api/v1/system/scan/status
```

## Audio Pipeline

```mermaid
flowchart LR
    SRC["Source<br>(file / stream URL)"]
    CHECK{"Output supports<br>source format?"}
    PASS["Passthrough<br>(bit-perfect)"]
    DSD{"DSD/DSF<br>source?"}
    DSDPASS["DSD Native<br>Passthrough"]
    DSDPCM["DSD → PCM<br>176.4kHz / 24-bit"]
    DECODE["FFmpeg Decode<br>→ PCM"]
    OUT["Output<br>(DLNA / AirPlay / Local)"]

    SRC --> CHECK
    CHECK -->|Yes| PASS --> OUT
    CHECK -->|No| DSD
    DSD -->|"Yes + DSD renderer"| DSDPASS --> OUT
    DSD -->|"Yes + PCM only"| DSDPCM --> OUT
    DSD -->|No| DECODE --> OUT
```

- **Passthrough** — Output supports the source format (e.g., DLNA playing FLAC): original bytes served directly. Bit-perfect, zero processing.
- **DSD Native** — DSF/DFF files sent bit-perfect to DSD-capable DLNA renderers (auto-detected). Fallback: PCM 176.4kHz/24-bit.
- **Decode** — FFmpeg decodes to PCM at the appropriate sample rate and bit depth. Never upsamples.

## Project Structure

```
tune_server/
├── app.py              # Bootstrap, wires all components
├── config.py           # Pydantic Settings
├── event_bus.py        # Async pub/sub (40 event types)
├── models.py           # Pydantic models
├── db/                 # SQLite + FTS5
├── library/            # Scanner, metadata, artwork, watcher
├── audio/              # Pipeline, decoder, encoder, buffer
├── playback/           # Queue, player state machine, gapless
├── zones/              # Zone manager, grouping, sync engine
├── outputs/            # DLNA, AirPlay, local, HTTP streamer
├── discovery/          # SSDP, mDNS, device registry
├── streaming/          # Tidal, Qobuz, YouTube, Amazon, Spotify, Deezer
├── api/                # FastAPI routes, WebSocket, deps
└── utils/              # Network, audio helpers
```

## Documentation

- [API Reference](docs/api-reference.md) — Full endpoint documentation (106+ endpoints)
- [Architecture](docs/architecture.md) — System design, component diagrams, data flows
- [Audio Pipeline](docs/audio-pipeline.md) — Decode, passthrough, and direct URL streaming
- [Event Bus](docs/event-bus.md) — 40 event types and pub/sub system
- [Database](docs/database.md) — Schema, FTS5, repository pattern
- [Device Discovery](docs/discovery.md) — SSDP (DLNA) and mDNS (AirPlay) scanning
- [Outputs](docs/outputs.md) — DLNA, AirPlay, and local output targets
- [Multi-Room](docs/multi-room.md) — Zone grouping and synchronized playback
- [Tidal Setup](docs/tidal-setup.md) — OAuth device code, quality levels
- [Qobuz Setup](docs/qobuz-setup.md) — App ID/Secret authentication, Hi-Res FLAC
- [YouTube Music Setup](docs/youtube-music-setup.md) — Google OAuth, device code flow
- [Amazon Music Setup](docs/amazon-music-setup.md) — OAuth device code, regions, quality
- [Spotify Setup](docs/spotify-setup.md) — OAuth PKCE, Free vs Premium
- [Deezer Setup](docs/deezer-setup.md) — OAuth 2.0, app ID/secret
- [Linux Deployment](docs/linux.md) — Audio, mDNS, firewall, troubleshooting
- [Project History](docs/project/index.md) — Development phases and decisions
- [Roon vs Tune](docs/roon-vs-tune.md) — Feature comparison

## Dependencies

| Library | Purpose |
|---------|---------|
| fastapi + uvicorn | REST API + WebSocket |
| aiosqlite | Async SQLite |
| mutagen | Audio metadata extraction |
| async-upnp-client | DLNA/UPnP control |
| pyatv | AirPlay streaming |
| zeroconf | mDNS discovery |
| sounddevice + numpy | Local audio output |
| aiohttp | HTTP audio server + HTTP client |
| watchfiles | Filesystem monitoring |
| structlog | Structured logging |
| pydantic-settings | Configuration |
| ytmusicapi | YouTube Music catalog browsing |
| yt-dlp | YouTube audio URL extraction |
| tidalapi | Tidal streaming |
| deezer-python | Deezer catalog browsing |

## License

Private — All rights reserved.

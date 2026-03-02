# Getting Started

A step-by-step guide to install Tune Server, configure a streaming service, and play your first track.

## 1. Install

### Quick Install (Debian/Ubuntu)

```bash
git clone git@github.com:renesenses/tune-server.git
cd tune-server
sudo ./install.sh --systemd
```

### Development Setup

```bash
git clone git@github.com:renesenses/tune-server.git
cd tune-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Install FFmpeg (required for audio decoding)
sudo apt install ffmpeg          # Debian/Ubuntu
# or: brew install ffmpeg        # macOS
```

## 2. Configure

Create a `.env` file at the project root:

```bash
# Point to your music library
TUNE_MUSIC_DIRS='["/home/user/Music"]'

# API and streaming ports
TUNE_API_PORT=8888
TUNE_STREAM_PORT=8080

# Enable device discovery (DLNA, AirPlay)
TUNE_DISCOVERY_ENABLED=true
```

## 3. Start the Server

```bash
# Development
python -m tune_server

# Production (systemd)
sudo systemctl start tune-server
```

The API is available at `http://localhost:8888`. Open it in a browser to access the web UI (if built).

## 4. Verify It Works

```bash
# Health check
curl localhost:8888/api/v1/system/health
# → {"status": "ok"}

# Check library stats
curl localhost:8888/api/v1/library/stats
# → {"tracks": 7491, "albums": 838, "artists": 402}
```

## 5. Add a Streaming Service (Optional)

### Tidal

Add to `.env`:

```bash
TUNE_TIDAL_ENABLED=true
TUNE_TIDAL_QUALITY=LOSSLESS  # or HI_RES_LOSSLESS
```

Restart, then authenticate via the device code flow:

```bash
curl -X POST localhost:8888/api/v1/streaming/tidal/auth
# → {"verification_url": "https://link.tidal.com/XXXXX", "user_code": "ABCDE"}
```

Open the URL, enter the code, authorize. Done.

### Qobuz

Add to `.env`:

```bash
TUNE_QOBUZ_ENABLED=true
TUNE_QOBUZ_APP_ID=your_app_id
TUNE_QOBUZ_APP_SECRET=your_app_secret
```

```bash
curl -X POST localhost:8888/api/v1/streaming/qobuz/auth \
  -H 'Content-Type: application/json' \
  -d '{"username": "user@example.com", "password": "secret"}'
```

See [Tidal Setup](tidal-setup.md), [Qobuz Setup](qobuz-setup.md), and other setup guides for detailed instructions.

## 6. Create a Zone

### Local Output (soundcard)

```bash
curl -X POST localhost:8888/api/v1/zones \
  -H 'Content-Type: application/json' \
  -d '{"name": "Desktop", "output_type": "local"}'
```

### DLNA Output (network renderer)

First, check discovered devices:

```bash
curl localhost:8888/api/v1/devices
# → [{"id": "uuid:...", "name": "DMP-A8", "type": "dlna", ...}]
```

Then create a zone with the device ID:

```bash
curl -X POST localhost:8888/api/v1/zones \
  -H 'Content-Type: application/json' \
  -d '{"name": "Living Room", "output_type": "dlna", "output_device_id": "uuid:9C41535E-..."}'
```

## 7. Play a Track

### From Local Library

```bash
# Search for a track
curl "localhost:8888/api/v1/library/search?q=radiohead"

# Play a track by ID on zone 1
curl -X POST localhost:8888/api/v1/zones/1/play \
  -H 'Content-Type: application/json' \
  -d '{"track_id": 42}'
```

### From a Streaming Service

```bash
# Search Tidal
curl "localhost:8888/api/v1/streaming/tidal/search?q=radiohead&limit=5"

# Play a Tidal track
curl -X POST localhost:8888/api/v1/zones/1/play \
  -H 'Content-Type: application/json' \
  -d '{"source": "tidal", "source_id": "12345678"}'
```

### Play an Album

```bash
curl -X POST localhost:8888/api/v1/zones/1/play \
  -H 'Content-Type: application/json' \
  -d '{"album_id": 10}'
```

## 8. Basic Playback Controls

```bash
# Pause
curl -X POST localhost:8888/api/v1/zones/1/pause

# Resume
curl -X POST localhost:8888/api/v1/zones/1/resume

# Skip to next track
curl -X POST localhost:8888/api/v1/zones/1/next

# Set volume (0.0 - 1.0)
curl -X POST localhost:8888/api/v1/zones/1/volume \
  -H 'Content-Type: application/json' \
  -d '{"volume": 0.7}'

# Check current status
curl localhost:8888/api/v1/zones/1/status
```

## 9. Multi-Room (Optional)

Group zones for synchronized playback:

```bash
# Group zone 1 (local) and zone 4 (DLNA), with zone 4 as leader
curl -X POST localhost:8888/api/v1/zones/group \
  -H 'Content-Type: application/json' \
  -d '{"leader_id": 4, "zone_ids": [1, 4]}'
```

See [Multi-Room](multi-room.md) for details on sync configuration and per-zone offsets.

## Next Steps

- [API Reference](api-reference.md) — Full endpoint documentation
- [Architecture](architecture.md) — System design overview
- [Audio Pipeline](audio-pipeline.md) — How audio flows from source to speaker
- [Discovery](discovery.md) — DLNA/AirPlay device discovery
- [Multi-Room](multi-room.md) — Synchronized playback configuration

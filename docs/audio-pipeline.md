# Audio Pipeline

## Overview

The audio pipeline determines how audio data flows from a source file to an output device. The key decision is **passthrough vs. decode**.

## Decision Flow

```mermaid
flowchart TD
    SRC["Source file<br>(FLAC, AAC, MP3, WAV, ALAC, OGG, DSD, ...)"]
    DECIDE{"Can the output handle<br>this format natively?"}
    PASS["Passthrough (bit-perfect)<br>Original file bytes served directly<br>Zero CPU, zero quality loss"]
    DECODE["Decode via FFmpeg<br>Source → PCM (raw audio samples)<br>Downsample if output max rate < source rate<br>Never upsample"]

    SRC --> DECIDE
    DECIDE -->|YES| PASS
    DECIDE -->|NO| DECODE
```

## Passthrough Mode

Used when the output supports the source format. Example: DLNA renderer playing an AAC file.

```mermaid
flowchart LR
    FILE["AAC File<br>on disk"] -->|"read 4KB chunks"| BUF["Output Buffer<br>(AsyncRingBuffer)"]
    BUF --> HTTP["HTTP Streamer<br>(serve file)"]
    HTTP --> DMR["DLNA Renderer"]
```

- File is read in 4KB chunks via `asyncio.to_thread(f.read)`
- Chunks are pushed to `AsyncRingBuffer` (256 slots)
- For DLNA: the HTTP streamer serves the file directly with Range support
- `AudioStreamInfo` includes `file_size` for Content-Length headers

### Format Capabilities

```python
DLNA_CAPABILITIES = AudioCapabilities(
    formats={FLAC, WAV, MP3, AAC, OGG, ALAC},
    max_sample_rate=192000,
    max_bit_depth=24,
    supports_gapless=True,  # via SetNextAVTransportURI
)

LOCAL_CAPABILITIES = AudioCapabilities(
    formats={WAV},  # sounddevice only accepts raw PCM
    max_sample_rate=384000,
    max_bit_depth=32,
    supports_gapless=True,
)
```

## Decode Mode

Used when the output cannot handle the source format. Example: local soundcard playing an AAC file.

```mermaid
flowchart LR
    FILE["AAC File<br>on disk"] --> FF["FFmpeg Decoder<br>(subprocess)<br>-f s16le/s24le<br>-ar rate -ac channels<br>pipe:1"]
    FF -->|"PCM chunks"| LOOP["Decode Loop<br>(pipe chunks)"]
    LOOP --> BUF["Output Buffer"]
    BUF --> SD["sounddevice<br>callback"]
```

### FFmpeg Decoder

The decoder runs FFmpeg as an async subprocess:

```
ffmpeg -hide_banner -loglevel error [-ss <seek>] \
  -i <input_file> \
  -f s16le -ar 44100 -ac 2 -acodec pcm_s16le \
  pipe:1
```

- Output format: raw PCM (`s16le`, `s24le`, or `s32le`)
- Sample rate: min(source_rate, output_max_rate) — never upsamples
- Bit depth: min(source_depth, output_max_depth)
- Seek: `-ss` flag for FFmpeg input seeking (fast, uses keyframes)

### PCM Format Selection

| Source Bit Depth | PCM Output Format |
|-----------------|-------------------|
| ≤ 16 bits       | `s16le` (2 bytes/sample) |
| 17-24 bits      | `s24le` (3 bytes/sample) |
| > 24 bits       | `s32le` (4 bytes/sample) |

## Buffer Architecture

```mermaid
block-beta
    columns 1
    block:RING["AsyncRingBuffer"]
        columns 1
        A["Bounded async queue (max_chunks slots)"]
        B["put() blocks when full (backpressure)"]
        C["get() blocks when empty"]
        D["close() sends None sentinel"]
        E["reset() clears for reuse"]
    end
    block:SIZES
        columns 2
        F["Decoder buffer:<br>128 chunks (512KB)"]
        G["Output buffer:<br>256 chunks (1MB)"]
    end
```

The buffer provides backpressure: if the output can't consume fast enough, the decoder slows down naturally. If the decoder is slower than real-time (shouldn't happen for local files), the output will briefly block.

## DLNA Streaming Details

For DLNA outputs, the HTTP Audio Streamer serves audio to the renderer:

### File Passthrough (most common)

```mermaid
sequenceDiagram
    participant DMR as DLNA Renderer
    participant HS as HTTP Streamer

    DMR->>HS: GET /stream/<id>.aac
    HS-->>DMR: 200 OK + file bytes
```

Supports:
- `Content-Length` header (renderer knows total size)
- `Range` requests (206 Partial Content for seeking)
- `Accept-Ranges: bytes`
- DLNA headers: `transferMode.dlna.org: Streaming`

### Streaming Mode (for transcoded audio)

When audio is transcoded (PCM from decoder → re-encoded for DLNA), chunks are pushed to the stream session queue and served as a chunked response. No Content-Length, no Range support.

## Seek Implementation

```mermaid
flowchart TD
    SEEK["Player.seek(position_ms)"]
    STOP["Stop current pipeline<br>(cancel tasks, kill FFmpeg)"]
    START["Start new pipeline<br>with seek_ms parameter"]
    PASS["Passthrough: not supported<br>(seek_ms > 0 forces decode mode)"]
    DEC["Decode: FFmpeg -ss flag<br>Seeks to nearest keyframe, then decodes"]

    SEEK --> STOP
    SEEK --> START
    START --> PASS
    START --> DEC
```

Note: seeking in passthrough mode is not possible (can't serve partial file from arbitrary position in a compressed format). The pipeline automatically falls back to decode mode when `seek_ms > 0`.

## Gapless Playback

### Local Output
Pre-decode the next track while the current track plays. When the current track ends, the next track's PCM is already buffered.

### DLNA Output
Use `SetNextAVTransportURI` to tell the renderer what to play next. The renderer handles the transition internally.

```mermaid
flowchart TD
    ADV["Player._advance_track()"]
    NEXT["queue.next() → next Track"]
    START["_start_track(next_track)"]
    PO["Pipeline + Output start<br>for next track"]

    ADV --> NEXT --> START --> PO
```

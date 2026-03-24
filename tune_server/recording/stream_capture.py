"""Parallel stream download — captures source audio without affecting playback."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Optional, Callable

import aiohttp
import structlog

logger = structlog.get_logger()


async def download_stream(
    url: str,
    output_path: Path,
    *,
    on_progress: Optional[Callable[[int], None]] = None,
    timeout: int = 600,
    stop_event: Optional[asyncio.Event] = None,
) -> int:
    """Download a stream URL to a file. Returns bytes written.

    Works for:
    - Qobuz/Tidal FLAC CDN URLs
    - UPnP media server res_url
    - Any direct HTTP(S) audio URL
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = 0

    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=15)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise IOError(f"HTTP {resp.status} for {url[:80]}")

            with open(output_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(65536):
                    if stop_event and stop_event.is_set():
                        break
                    f.write(chunk)
                    bytes_written += len(chunk)
                    if on_progress:
                        on_progress(bytes_written)

    logger.info("stream_downloaded", path=str(output_path), bytes=bytes_written)
    return bytes_written


async def capture_radio_stream(
    url: str,
    output_dir: Path,
    *,
    on_track: Optional[Callable[[str, str, Path], None]] = None,
    stop_event: Optional[asyncio.Event] = None,
    max_duration_s: int = 7200,  # 2 hours default
) -> list[Path]:
    """Capture a radio stream with ICY metadata splitting.

    Returns list of saved file paths (one per detected track).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    saved_files: list[Path] = []
    current_file = None
    current_title = ""
    track_num = 0
    start_time = asyncio.get_event_loop().time()

    headers = {"Icy-MetaData": "1", "User-Agent": "TuneRecorder/1.0"}

    client_timeout = aiohttp.ClientTimeout(total=max_duration_s + 60, connect=15)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise IOError(f"HTTP {resp.status} for radio stream")

                metaint = int(resp.headers.get("icy-metaint", "0"))
                content_type = resp.headers.get("content-type", "audio/mpeg")

                # Determine file extension from content type
                ext = _ext_from_content_type(content_type)

                if metaint == 0:
                    # No ICY metadata — save as single continuous file
                    file_path = output_dir / f"recording{ext}"
                    saved_files.append(file_path)
                    with open(file_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            if stop_event and stop_event.is_set():
                                break
                            elapsed = asyncio.get_event_loop().time() - start_time
                            if elapsed > max_duration_s:
                                break
                            f.write(chunk)
                    return saved_files

                # ICY metadata-aware capture
                buffer = bytearray()
                audio_bytes_since_meta = 0

                async for raw_chunk in resp.content.iter_any():
                    if stop_event and stop_event.is_set():
                        break
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > max_duration_s:
                        break

                    buffer.extend(raw_chunk)

                    while len(buffer) > 0:
                        remaining_audio = metaint - audio_bytes_since_meta

                        if remaining_audio > 0:
                            # Read audio bytes
                            audio_chunk = bytes(buffer[:remaining_audio])
                            buffer = buffer[len(audio_chunk):]
                            audio_bytes_since_meta += len(audio_chunk)

                            if current_file:
                                current_file.write(audio_chunk)
                            continue

                        # We've reached a metadata boundary
                        if len(buffer) < 1:
                            break  # need more data

                        meta_length = buffer[0] * 16
                        if len(buffer) < 1 + meta_length:
                            break  # need more data

                        # Parse metadata
                        meta_bytes = bytes(buffer[1:1 + meta_length])
                        buffer = buffer[1 + meta_length:]
                        audio_bytes_since_meta = 0

                        meta_str = meta_bytes.decode("utf-8", errors="ignore").rstrip("\x00")
                        new_title = _parse_icy_title(meta_str)

                        if new_title and new_title != current_title:
                            # Track changed — finalize current file, start new one
                            if current_file:
                                current_file.close()
                                logger.info("radio_track_saved",
                                            title=current_title,
                                            path=str(saved_files[-1]))

                            current_title = new_title
                            track_num += 1
                            safe_name = _safe_filename(new_title)
                            file_path = output_dir / f"{track_num:03d} - {safe_name}{ext}"
                            saved_files.append(file_path)
                            current_file = open(file_path, "wb")

                            if on_track:
                                artist, title = _split_artist_title(new_title)
                                on_track(artist, title, file_path)

                        elif not current_title and not current_file:
                            # First audio before any metadata
                            file_path = output_dir / f"000 - unknown{ext}"
                            saved_files.append(file_path)
                            current_file = open(file_path, "wb")

    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("radio_capture_error", error=str(e))
    finally:
        if current_file:
            current_file.close()

    return saved_files


def copy_local_file(source: str, output_path: Path) -> int:
    """Copy a local file to the recording directory."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output_path)
    return output_path.stat().st_size


def _ext_from_content_type(ct: str) -> str:
    ct = ct.lower().split(";")[0].strip()
    mapping = {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/aac": ".aac",
        "audio/aacp": ".aac",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/flac": ".flac",
        "audio/x-flac": ".flac",
        "application/ogg": ".ogg",
    }
    return mapping.get(ct, ".mp3")


def _parse_icy_title(meta: str) -> str:
    """Extract StreamTitle from ICY metadata string."""
    for part in meta.split(";"):
        part = part.strip()
        if part.lower().startswith("streamtitle="):
            title = part.split("=", 1)[1].strip("'\"")
            return title
    return ""


def _split_artist_title(icy_title: str) -> tuple[str, str]:
    """Split 'Artist - Title' into (artist, title)."""
    if " - " in icy_title:
        artist, title = icy_title.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", icy_title.strip()


def _safe_filename(name: str, max_len: int = 100) -> str:
    """Convert a string to a safe filename."""
    import re
    safe = re.sub(r'[<>:"/\\|?*]', '_', name)
    safe = safe.strip(". ")
    return safe[:max_len] if safe else "unknown"

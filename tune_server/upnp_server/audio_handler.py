"""Serve audio files by track ID for UPnP control points."""
from __future__ import annotations

import os
from pathlib import Path

import structlog
from aiohttp import web

from tune_server.audio.formats import mime_type_for_format
from tune_server.models import AudioFormat

logger = structlog.get_logger()


def register_audio_routes(app: web.Application, track_repo) -> None:
    async def handle_audio(request: web.Request) -> web.Response:
        track_id = request.match_info.get("track_id")
        if not track_id:
            return web.Response(status=400)

        try:
            track = await track_repo.get(int(track_id))
        except Exception:
            return web.Response(status=404)

        if not track or not track.file_path:
            return web.Response(status=404)

        file_path = Path(track.file_path)
        if not file_path.exists():
            return web.Response(status=404)

        fmt = AudioFormat(track.format) if track.format else AudioFormat.FLAC
        mime = mime_type_for_format(fmt)
        file_size = os.path.getsize(file_path)

        # Range request support
        range_header = request.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            range_spec = range_header[6:]
            start_str, _, end_str = range_spec.partition("-")
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1

            resp = web.StreamResponse(
                status=206,
                headers={
                    "Content-Type": mime,
                    "Content-Length": str(length),
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "transferMode.dlna.org": "Streaming",
                },
            )
            await resp.prepare(request)
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    await resp.write(chunk)
                    remaining -= len(chunk)
            await resp.write_eof()
            return resp

        # Full file
        resp = web.StreamResponse(
            headers={
                "Content-Type": mime,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "transferMode.dlna.org": "Streaming",
            },
        )
        await resp.prepare(request)
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                await resp.write(chunk)
        await resp.write_eof()
        return resp

    app.router.add_get("/upnp/audio/{track_id}", handle_audio)

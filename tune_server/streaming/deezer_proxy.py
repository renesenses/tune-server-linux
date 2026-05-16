"""HTTP proxy that decrypts Deezer streams on the fly.

Deezer audio URLs serve Blowfish-CBC-encrypted bytes. DLNA renderers
cannot decrypt — they would just play noise. We expose a local endpoint
on the existing audio streamer port (default 8080) so the renderer
fetches a *plain* stream from us; we fetch encrypted from Deezer in the
background and pipe decrypted audio through.

Route: GET /deezer/{sng_id}.{ext}  (ext is informational, e.g. .flac)
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

import aiohttp
import structlog
from aiohttp import web

from tune_server.streaming.deezer_decrypt import compute_blowfish_key, decrypt_chunk

if TYPE_CHECKING:
    from tune_server.streaming.deezer import DeezerService

logger = structlog.get_logger()

_CHUNK = 2048


class DeezerProxy:
    """Decrypting proxy for Deezer encrypted audio streams."""

    def __init__(self, service: "DeezerService") -> None:
        self._service = service

    def register_routes(self, app: web.Application) -> None:
        app.router.add_get("/deezer/{filename}", self._handle_get)

    @staticmethod
    def proxy_url_for(server_ip: str, port: int, sng_id: str, ext: str = "flac") -> str:
        return f"http://{server_ip}:{port}/deezer/{sng_id}.{ext}"

    async def _handle_get(self, request: web.Request) -> web.StreamResponse:
        filename = request.match_info["filename"]
        # Extract SNG_ID from "{sng_id}.{ext}"
        sng_id = filename.rsplit(".", 1)[0]
        if not sng_id.isdigit():
            return web.Response(status=400, text="invalid sng_id")

        # Resolve a fresh upstream URL via the deezer service.
        upstream = await self._service._get_full_stream_url(sng_id)
        if not upstream:
            logger.warning("deezer_proxy_no_upstream", sng_id=sng_id)
            return web.Response(status=404, text="upstream not available")

        ext = filename.rsplit(".", 1)[1] if "." in filename else "flac"
        content_type = {
            "flac": "audio/flac",
            "mp3": "audio/mpeg",
        }.get(ext.lower(), "application/octet-stream")

        headers = {
            "Content-Type": content_type,
            "Accept-Ranges": "none",
            "transferMode.dlna.org": "Streaming",
        }
        try:
            async with aiohttp.ClientSession() as head_session:
                async with head_session.head(upstream, timeout=aiohttp.ClientTimeout(total=10)) as head_resp:
                    cl = head_resp.headers.get("Content-Length")
                    if cl:
                        headers["Content-Length"] = cl
        except Exception:
            pass
        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)

        key = compute_blowfish_key(sng_id)
        try:
            stream_timeout = aiohttp.ClientTimeout(total=900, connect=15, sock_read=120)
            session = aiohttp.ClientSession(timeout=stream_timeout)
            async with session, session.get(upstream) as upstream_resp:
                if upstream_resp.status != 200:
                    logger.warning("deezer_proxy_upstream_status",
                                   sng_id=sng_id, status=upstream_resp.status)
                    return response
                buffer = bytearray()
                chunk_index = 0
                async for piece in upstream_resp.content.iter_chunked(8192):
                    if not piece:
                        continue
                    buffer.extend(piece)
                    while len(buffer) >= _CHUNK:
                        chunk = bytes(buffer[:_CHUNK])
                        del buffer[:_CHUNK]
                        if chunk_index % 3 == 0:
                            await response.write(decrypt_chunk(chunk, key))
                        else:
                            await response.write(chunk)
                        chunk_index += 1
                if buffer:
                    await response.write(bytes(buffer))
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception as exc:
            logger.warning("deezer_proxy_stream_error", sng_id=sng_id, error=str(exc))
        return response

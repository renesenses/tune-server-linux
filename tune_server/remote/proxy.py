"""Reverse proxy — forward all API + WebSocket requests to a remote Tune Server."""
from __future__ import annotations

import asyncio

import aiohttp
import structlog
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from starlette.websockets import WebSocketState

from tune_server.config import settings

logger = structlog.get_logger()


def create_remote_app(remote_base: str) -> FastAPI:
    """Create a FastAPI app that proxies everything to a remote Tune Server.

    The web client is served locally (for speed), but all API calls and
    WebSocket connections are forwarded to the remote server.
    """
    from tune_server import __version__

    app = FastAPI(
        title="Tune Remote",
        version=__version__,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Remote server info endpoint ─────────────────────────────────────
    @app.get("/api/v1/remote/status")
    async def remote_status():
        return {
            "mode": "remote",
            "remote_host": remote_base,
            "version": __version__,
        }

    # ── Remote server discovery endpoint ────────────────────────────────
    @app.get("/api/v1/remote/discover")
    async def remote_discover():
        from tune_server.remote.discovery import discover_tune_servers
        servers = await discover_tune_servers(timeout=5)
        return [
            {"name": s.name, "host": s.host, "port": s.port, "uuid": s.uuid}
            for s in servers
        ]

    # ── Remote server switch ────────────────────────────────────────────
    @app.post("/api/v1/remote/connect")
    async def remote_connect(request: Request):
        data = await request.json()
        host = data.get("host")
        port = data.get("port", 8888)
        if not host:
            return {"error": "host required"}
        # Persist to .env
        from tune_server.config import persist_env_var
        persist_env_var("TUNE_MODE", "remote")
        persist_env_var("TUNE_REMOTE_HOST", f"{host}:{port}")
        return {"status": "connected", "remote_host": f"{host}:{port}",
                "message": "Restart Tune Server to apply"}

    # ── WebSocket proxy ─────────────────────────────────────────────────
    @app.websocket("/ws")
    async def ws_proxy(ws: WebSocket):
        await ws.accept()
        ws_url = remote_base.replace("http://", "ws://").replace("https://", "wss://") + "/ws"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url) as remote_ws:

                    async def forward_to_client():
                        async for msg in remote_ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await ws.send_text(msg.data)
                            elif msg.type == aiohttp.WSMsgType.BINARY:
                                await ws.send_bytes(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                                break

                    async def forward_to_remote():
                        try:
                            while True:
                                data = await ws.receive_text()
                                await remote_ws.send_str(data)
                        except WebSocketDisconnect:
                            await remote_ws.close()

                    await asyncio.gather(
                        forward_to_client(),
                        forward_to_remote(),
                        return_exceptions=True,
                    )
        except Exception:
            logger.debug("ws_proxy_closed")
        finally:
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.close()

    # ── API proxy (catch-all) ───────────────────────────────────────────
    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def api_proxy(request: Request, path: str):
        target_url = f"{remote_base}/api/{path}"

        # Forward query params
        if request.query_params:
            target_url += f"?{request.query_params}"

        # Forward headers (skip host)
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "content-length", "transfer-encoding")}

        # Forward body
        body = await request.body()

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    data=body if body else None,
                ) as resp:
                    content = await resp.read()
                    # Forward response headers
                    resp_headers = {k: v for k, v in resp.headers.items()
                                    if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")}
                    return Response(
                        content=content,
                        status_code=resp.status,
                        headers=resp_headers,
                        media_type=resp.content_type,
                    )
        except aiohttp.ClientError as e:
            logger.warning("proxy_error", url=target_url, error=str(e))
            return Response(
                content=f'{{"detail":"Remote server unavailable: {e}"}}',
                status_code=502,
                media_type="application/json",
            )

    # ── Serve web client locally ────────────────────────────────────────
    if settings.web_dir:
        web_path = Path(settings.web_dir)
        if web_path.is_dir():
            assets_dir = web_path / "assets"
            if assets_dir.is_dir():
                app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

            @app.get("/{path:path}")
            async def serve_web(path: str):
                # Try static file first
                file_path = web_path / path
                if file_path.is_file() and ".." not in path:
                    return FileResponse(str(file_path))
                # Fallback to index.html (SPA)
                index = web_path / "index.html"
                if index.is_file():
                    return FileResponse(str(index))
                return Response(status_code=404)

    return app

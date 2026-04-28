from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from starlette.types import ASGIApp, Receive, Scope, Send

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from tune_server.api.deps import deps
from tune_server.api.routes import artist_metadata, devices, dj, library, network, party, playback, playlist_manager, playlists, podcasts, profiles, radio_favorites, radios, search, spotify_connect, streaming, system, zone_manager, zones
from tune_server.api.routes import metadata
from tune_server.api.websocket import WebSocketManager
from tune_server.config import settings

_ws_manager: WebSocketManager | None = None


def create_api_app() -> FastAPI:
    from tune_server import __version__
    app = FastAPI(
        title="Tune Server",
        description="Network-accessible music server with multi-room playback",
        version=__version__,
    )

    # CORS — use configured origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials="*" not in settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Catch-all exception handler so unhandled errors don't disappear into
    # a generic 500. We log the path + repr(exception) so Windows testers'
    # crashes are visible in tune-server.log.
    import structlog as _sl
    _api_log = _sl.get_logger()

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request, exc):
        _api_log.error(
            "api_unhandled_exception",
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(exc).__name__}: {str(exc)[:200]}"},
        )

    # API key middleware (if configured)
    if settings.api_key:
        @app.middleware("http")
        async def check_api_key(request, call_next):
            path = request.url.path
            # Always open: root, docs, health check
            if path in ("/", "/docs", "/openapi.json", "/api/v1/system/health"):
                return await call_next(request)
            # Static web UI assets (CSS/JS/images)
            if not path.startswith("/api/") and path != "/ws":
                return await call_next(request)

            key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
            if key != settings.api_key:
                return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
            return await call_next(request)

    # Mount routes under /api/v1
    app.include_router(library.router, prefix="/api/v1")
    app.include_router(playlists.router, prefix="/api/v1")
    app.include_router(playback.router, prefix="/api/v1")
    app.include_router(playback.global_router, prefix="/api/v1")
    app.include_router(zones.router, prefix="/api/v1")
    app.include_router(devices.router, prefix="/api/v1")
    app.include_router(streaming.router, prefix="/api/v1")
    app.include_router(search.router, prefix="/api/v1")
    app.include_router(system.router, prefix="/api/v1")
    app.include_router(network.router, prefix="/api/v1")
    app.include_router(radios.router, prefix="/api/v1")
    app.include_router(radio_favorites.router, prefix="/api/v1")
    app.include_router(profiles.router, prefix="/api/v1")
    app.include_router(podcasts.router, prefix="/api/v1")
    app.include_router(playlist_manager.router, prefix="/api/v1")
    app.include_router(party.router, prefix="/api/v1")
    app.include_router(dj.router, prefix="/api/v1")
    app.include_router(zone_manager.router, prefix="/api/v1")
    app.include_router(metadata.router, prefix="/api/v1")
    app.include_router(artist_metadata.router, prefix="/api/v1")
    app.include_router(spotify_connect.router, prefix="/api/v1")
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        global _ws_manager
        if _ws_manager:
            await _ws_manager.handle_websocket(websocket)

    # Serve built web UI if configured
    web_dir = Path(settings.web_dir) if settings.web_dir else None
    if web_dir and web_dir.is_dir():
        # Serve /assets/ (Vite hashed files)
        assets_dir = web_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        index_html = web_dir / "index.html"

        # SPA fallback middleware: serves index.html for non-API GET 404s
        class _SPAFallback:
            """Wraps the FastAPI app. For non-API GET requests that would 404,
            serve index.html instead (SPA client-side routing)."""

            def __init__(self, app: ASGIApp) -> None:
                self.app = app

            async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                if scope["type"] != "http":
                    await self.app(scope, receive, send)
                    return

                path = scope["path"]
                method = scope.get("method", "GET")

                # Non-GET or API/docs/ws: always pass through
                if method != "GET" or path.startswith(("/api/", "/docs", "/openapi.json", "/ws")):
                    await self.app(scope, receive, send)
                    return

                # Serve static file from web_dir if it exists (favicon, etc.)
                rel = path.lstrip("/")
                if rel:
                    file_path = web_dir / rel
                    if file_path.is_file() and file_path.resolve().is_relative_to(web_dir.resolve()):
                        response = FileResponse(file_path)
                        await response(scope, receive, send)
                        return

                # Let FastAPI try first; if it 404s, serve index.html
                status_code = 0
                headers_sent = False
                buffered: list = []

                async def _intercept_send(message):
                    nonlocal status_code, headers_sent
                    if message["type"] == "http.response.start":
                        status_code = message.get("status", 200)
                        if status_code != 404:
                            headers_sent = True
                            await send(message)
                        else:
                            buffered.append(message)
                    elif message["type"] == "http.response.body":
                        if headers_sent:
                            await send(message)
                        else:
                            buffered.append(message)

                await self.app(scope, receive, _intercept_send)

                if status_code == 404 and not headers_sent:
                    # Serve SPA index.html instead of 404
                    response = FileResponse(index_html)
                    await response(scope, receive, send)
                elif not headers_sent and buffered:
                    # Flush buffered messages (shouldn't happen)
                    for msg in buffered:
                        await send(msg)

        app = _SPAFallback(app)  # type: ignore[assignment]
    else:
        @app.get("/")
        async def root():
            from tune_server import __version__
            return {
                "name": "Tune Server",
                "version": __version__,
                "api": "/api/v1",
                "docs": "/docs",
            }

    return app


async def setup_websocket_manager(event_bus) -> WebSocketManager:
    global _ws_manager
    _ws_manager = WebSocketManager(event_bus)
    await _ws_manager.start()
    return _ws_manager

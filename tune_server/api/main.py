from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from tune_server.api.deps import deps
from tune_server.api.routes import devices, library, playback, playlists, search, streaming, system, zones
from tune_server.api.websocket import WebSocketManager
from tune_server.config import settings

_ws_manager: WebSocketManager | None = None


def create_api_app() -> FastAPI:
    app = FastAPI(
        title="Tune Server",
        description="Network-accessible music server with multi-room playback",
        version="0.1.0",
    )

    # CORS — use configured origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials="*" not in settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API key middleware (if configured)
    if settings.api_key:
        @app.middleware("http")
        async def check_api_key(request, call_next):
            # Skip auth for docs, health, websocket, and static files
            path = request.url.path
            if path in ("/", "/docs", "/openapi.json", "/api/v1/system/health"):
                return await call_next(request)
            if path == "/ws":
                return await call_next(request)
            if not path.startswith("/api/"):
                return await call_next(request)

            key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
            if key != settings.api_key:
                return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
            return await call_next(request)

    # Mount routes under /api/v1
    app.include_router(library.router, prefix="/api/v1")
    app.include_router(playlists.router, prefix="/api/v1")
    app.include_router(playback.router, prefix="/api/v1")
    app.include_router(zones.router, prefix="/api/v1")
    app.include_router(devices.router, prefix="/api/v1")
    app.include_router(streaming.router, prefix="/api/v1")
    app.include_router(search.router, prefix="/api/v1")
    app.include_router(system.router, prefix="/api/v1")

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

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            # Try static file first
            file_path = web_dir / full_path
            if full_path and file_path.is_file() and file_path.resolve().is_relative_to(web_dir.resolve()):
                return FileResponse(file_path)
            # Fallback to index.html (SPA routing)
            return FileResponse(index_html)
    else:
        @app.get("/")
        async def root():
            return {
                "name": "Tune Server",
                "version": "0.1.0",
                "api": "/api/v1",
                "docs": "/docs",
            }

    return app


async def setup_websocket_manager(event_bus) -> WebSocketManager:
    global _ws_manager
    _ws_manager = WebSocketManager(event_bus)
    await _ws_manager.start()
    return _ws_manager

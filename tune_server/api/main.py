from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
            # Skip auth for docs, health, and websocket
            if request.url.path in ("/", "/docs", "/openapi.json", "/api/v1/system/health"):
                return await call_next(request)
            if request.url.path == "/ws":
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

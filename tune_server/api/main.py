from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from starlette.types import ASGIApp, Receive, Scope, Send

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from tune_server.api.deps import deps
from tune_server.api.routes import admin, alarms, artist_metadata, bug_report, dashboard, devices, dj, export, import_export, library, network, party, peers, playback, playlist_manager, playlist_sync, playlists, plugins, podcasts, profiles, radio_favorites, radios, search, services, smart_collections, spotify_connect, streaming, system, tags, zone_manager, zones
from tune_server.api.routes import metadata
from tune_server.api.websocket import WebSocketManager
from tune_server.config import settings

_ws_manager: WebSocketManager | None = None


def create_api_app() -> FastAPI:
    from tune_server import __version__

    openapi_tags = [
        {"name": "system", "description": "Server health, configuration, restart, updates, and diagnostics."},
        {"name": "library", "description": "Browse and manage the local music library (artists, albums, tracks, genres)."},
        {"name": "playback", "description": "Transport controls (play, pause, seek, next, previous) and queue management per zone."},
        {"name": "zones", "description": "Create, configure, and manage playback zones (DLNA, AirPlay, local)."},
        {"name": "streaming", "description": "Authenticate and browse streaming services (Tidal, Qobuz, Deezer, Spotify, YouTube Music, Amazon Music)."},
        {"name": "playlists", "description": "Create, edit, and manage playlists (local and streaming service playlists)."},
        {"name": "devices", "description": "Discover and manage audio output devices on the network."},
        {"name": "network", "description": "Network shares (SMB/NFS), mount management, and media server browsing."},
        {"name": "admin", "description": "Administrative operations: cache control, database maintenance, and server management."},
        {"name": "plugins", "description": "List, enable, disable, and configure server plugins."},
        {"name": "plugin-store", "description": "Browse and install plugins from the plugin store."},
        {"name": "tags", "description": "User-defined tags for organizing library items."},
        {"name": "alarm", "description": "Schedule alarm playback on zones."},
        {"name": "profiles", "description": "User profiles, favorites, and listening preferences."},
        {"name": "dashboard", "description": "Listening statistics and activity dashboard."},
        {"name": "search", "description": "Federated search across local library and streaming services."},
        {"name": "metadata", "description": "Track and album metadata editing, suggestions, and batch operations."},
        {"name": "artist-metadata", "description": "Artist biographies, images, and enrichment from MusicBrainz/Last.fm."},
        {"name": "radios", "description": "Internet radio station search and playback."},
        {"name": "radio-favorites", "description": "Manage favorite radio stations."},
        {"name": "playlist-manager", "description": "Cross-service playlist operations: transfer, sync, and merge playlists."},
        {"name": "playlist-sync", "description": "Automated playlist synchronisation between services."},
        {"name": "zone-manager", "description": "Zone group management: create and dissolve multi-room groups, stereo pairing."},
        {"name": "smart-collections", "description": "Dynamic smart collections based on rules (genre, year, rating, etc.)."},
        {"name": "services", "description": "Streaming service connection status and configuration."},
        {"name": "peers", "description": "Discover and relay to other Tune Server instances on the network."},
        {"name": "export", "description": "Export library data (albums, tracks, artists) as CSV."},
        {"name": "import", "description": "Import library data from external sources."},
        {"name": "podcasts", "description": "Podcast search and playback."},
        {"name": "dj", "description": "AI-assisted DJ mode for automatic playlist generation."},
        {"name": "party", "description": "Party mode: collaborative queue with guest access."},
        {"name": "spotify-connect", "description": "Spotify Connect integration."},
    ]

    app = FastAPI(
        title="Tune Server API",
        description=(
            "REST API for **Tune**, an open-source multi-room music server.\n\n"
            "Tune manages a local audio library, connects to streaming services "
            "(Tidal, Qobuz, YouTube Music, Spotify, Deezer, Amazon Music), "
            "and streams to DLNA/UPnP renderers, AirPlay devices, or local soundcards.\n\n"
            "**Base path:** `/api/v1`  \n"
            "**WebSocket:** `ws://<host>:8888/ws` for real-time events  \n"
            "**Web client:** served at `/` when a web bundle is present"
        ),
        version=__version__,
        openapi_tags=openapi_tags,
        docs_url="/docs",
        redoc_url="/redoc",
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
            # Always open: root, docs, redoc, health check, API landing
            if path in ("/", "/api", "/docs", "/redoc", "/openapi.json", "/api/v1/system/health"):
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
    app.include_router(playlist_sync.router, prefix="/api/v1")
    app.include_router(party.router, prefix="/api/v1")
    app.include_router(dj.router, prefix="/api/v1")
    app.include_router(zone_manager.router, prefix="/api/v1")
    app.include_router(metadata.router, prefix="/api/v1")
    app.include_router(artist_metadata.router, prefix="/api/v1")
    app.include_router(spotify_connect.router, prefix="/api/v1")
    app.include_router(smart_collections.router, prefix="/api/v1")
    app.include_router(services.router, prefix="/api/v1")
    app.include_router(peers.router, prefix="/api/v1")
    app.include_router(alarms.router, prefix="/api/v1")
    app.include_router(alarms.legacy_router, prefix="/api/v1")
    app.include_router(export.router, prefix="/api/v1")
    app.include_router(import_export.router, prefix="/api/v1")
    app.include_router(dashboard.router, prefix="/api/v1")
    app.include_router(bug_report.router, prefix="/api/v1")
    app.include_router(plugins.router, prefix="/api/v1")
    app.include_router(admin.router, prefix="/api/v1")
    app.include_router(tags.router, prefix="/api/v1")
    app.include_router(tags.library_router, prefix="/api/v1")
    from tune_server.api.routes import plugin_store
    app.include_router(plugin_store.router, prefix="/api/v1")
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        global _ws_manager
        if _ws_manager:
            await _ws_manager.handle_websocket(websocket)

    # API landing page — always available
    @app.get("/api", include_in_schema=False)
    async def api_landing():
        from tune_server import __version__
        return HTMLResponse(f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tune Server API</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0f1117; color: #e0e0e0; min-height: 100vh;
         display: flex; align-items: center; justify-content: center; }}
  .card {{ background: #1a1d27; border-radius: 16px; padding: 48px;
           max-width: 520px; width: 90%; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }}
  h1 {{ font-size: 28px; margin-bottom: 8px; color: #fff; }}
  .version {{ color: #888; font-size: 14px; margin-bottom: 32px; }}
  .links {{ display: flex; flex-direction: column; gap: 12px; }}
  a {{ display: flex; align-items: center; gap: 12px; padding: 16px 20px;
       background: #252833; border-radius: 10px; color: #e0e0e0;
       text-decoration: none; transition: background 0.15s; }}
  a:hover {{ background: #2d3142; }}
  .icon {{ font-size: 22px; width: 32px; text-align: center; }}
  .label {{ font-weight: 600; font-size: 15px; }}
  .hint {{ font-size: 12px; color: #888; margin-top: 2px; }}
</style>
</head>
<body>
<div class="card">
  <h1>Tune Server API</h1>
  <div class="version">v{__version__}</div>
  <div class="links">
    <a href="/docs">
      <span class="icon">&#9889;</span>
      <div>
        <div class="label">Swagger UI</div>
        <div class="hint">Interactive API explorer &mdash; try endpoints directly</div>
      </div>
    </a>
    <a href="/redoc">
      <span class="icon">&#128214;</span>
      <div>
        <div class="label">ReDoc</div>
        <div class="hint">Clean, readable API reference documentation</div>
      </div>
    </a>
    <a href="/openapi.json">
      <span class="icon">&#123;&#125;</span>
      <div>
        <div class="label">OpenAPI Schema</div>
        <div class="hint">Raw JSON schema for code generation and tooling</div>
      </div>
    </a>
    <a href="/">
      <span class="icon">&#127925;</span>
      <div>
        <div class="label">Web Client</div>
        <div class="hint">Open the Tune web interface</div>
      </div>
    </a>
  </div>
</div>
</body>
</html>""")

    # Root info route (when no web bundle is served)
    web_dir = Path(settings.web_dir) if settings.web_dir else None
    if not (web_dir and web_dir.is_dir()):
        @app.get("/", include_in_schema=False)
        async def root():
            from tune_server import __version__
            return {
                "name": "Tune Server",
                "version": __version__,
                "api": "/api/v1",
                "docs": "/docs",
                "redoc": "/redoc",
            }

    return app


def wrap_for_serving(app: FastAPI) -> ASGIApp:
    """Wrap the FastAPI app with the SPA fallback middleware (if a web bundle
    exists). Called by TuneServer AFTER plugins have mounted their routers,
    so the wrapper is the outermost layer for uvicorn to serve.

    Plugins must NEVER receive the wrapped app — they need the bare FastAPI
    so they can call ``include_router()``. ``create_api_app()`` returns the
    bare app; this function is only used at the very end of boot.
    """
    web_dir = Path(settings.web_dir) if settings.web_dir else None
    if not (web_dir and web_dir.is_dir()):
        return app

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

            # Non-GET or API/docs/redoc/ws: always pass through
            if method != "GET" or path.startswith(("/api", "/docs", "/redoc", "/openapi.json", "/ws")):
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

    return _SPAFallback(app)


async def setup_websocket_manager(event_bus) -> WebSocketManager:
    global _ws_manager
    _ws_manager = WebSocketManager(event_bus)
    await _ws_manager.start()
    return _ws_manager

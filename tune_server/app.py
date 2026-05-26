from __future__ import annotations

import asyncio
import logging
import time

import structlog
import uvicorn

from tune_server.api.deps import deps
from tune_server.api.main import create_api_app, setup_websocket_manager, wrap_for_serving
from tune_server.config import settings
from tune_server.db.factory import create_database
from tune_server.db.sa_repository import (
    TrackCreditRepo,
    AlbumRepo,
    ArtistRepo,
    PlaylistRepo,
    PlayQueueRepo,
    RadioFavoriteRepo,
    RadioStationRepo,
    TrackRepo,
    ZoneRepo,
)
from tune_server.discovery.manager import DiscoveryManager
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.library.enrichment import MetadataEnricher
from tune_server.library.scanner import LibraryScanner
from tune_server.library.watcher import FileSystemWatcher
from tune_server.outputs.http_streamer import HttpAudioStreamer
from tune_server.outputs.rust_streamer import RustSidecarStreamer, create_streamer
from tune_server.utils.audio_utils import check_ffmpeg
from tune_server.utils.network import get_local_ip, pick_free_port
from tune_server.zones.group import GroupManager
from tune_server.zones.manager import ZoneManager
from tune_server.zones.sync import SyncEngine

logger = structlog.get_logger()

COMPONENT_SHUTDOWN_TIMEOUT = 5  # seconds


class _TeeStream:
    """Forward writes to multiple streams, ignore failures.

    Used to send all structlog/uvicorn output to BOTH the original stdout
    (visible in console / launchers) AND a log file next to the running
    binary. On Windows --noconsole the original stdout is /dev/null so the
    file becomes the only place to read logs from.

    Implements the subset of TextIOBase that uvicorn / logging.config /
    structlog actually probe: isatty, fileno, encoding, errors, closed,
    writable/readable.
    """

    encoding = "utf-8"
    errors = None
    closed = False

    def __init__(self, *streams) -> None:
        self._streams = [s for s in streams if s is not None]

    def write(self, data) -> int:
        for s in self._streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        return len(data) if isinstance(data, str) else 0

    def flush(self) -> None:
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        # logging.StreamHandler probes this for color decisions; False is
        # safe since the file half is never a TTY.
        return False

    def fileno(self) -> int:
        # uvicorn's --reload occasionally probes for a file descriptor.
        # Defer to the first underlying stream that has one.
        for s in self._streams:
            try:
                return s.fileno()
            except Exception:
                continue
        import io
        raise io.UnsupportedOperation("fileno")

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def close(self) -> None:
        # Don't close the underlying streams — they may be sys.__stdout__.
        pass


def _resolve_log_path() -> "Path | None":
    """Pick a log file location based on platform / packaging mode."""
    import sys
    from pathlib import Path
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        # macOS .app bundles: writing inside the bundle pollutes
        # Contents/Resources/runtime/ and invalidates the codesign seal —
        # Gatekeeper then shows a "unverified" half-circle badge on the
        # app in Finder even though the app still runs. Route logs to
        # ~/Library/Logs/Tune Server/ instead, which is the macOS
        # convention anyway.
        if sys.platform == "darwin" and ".app/Contents/" in str(exe_dir):
            candidates.append(Path.home() / "Library" / "Logs" / "Tune Server" / "tune-server.log")
        elif sys.platform == "win32":
            # Windows: _ensure_data_dir() already set CWD to %APPDATA%/TuneServer/.
            # Log there so all user data (db, .env, logs) lives in one place.
            # Fall back to exe dir if the data dir is not writable.
            import os
            appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
            candidates.append(Path(appdata) / "TuneServer" / "tune-server.log")
            candidates.append(exe_dir / "tune-server.log")
        else:
            # PyInstaller bundle (Linux binary): log next to the running binary.
            candidates.append(exe_dir / "tune-server.log")
    candidates.append(Path.home() / ".tune" / "tune-server.log")
    candidates.append(Path("tune-server.log"))
    for p in candidates:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # Touch the file to confirm writability.
            with p.open("a", encoding="utf-8"):
                pass
            return p
        except Exception:
            continue
    return None


def _install_file_logging() -> None:
    """Tee stdout/stderr to a log file so launchers (Windows .bat, journalctl
    on Linux) can show users what happened on crash."""
    import sys
    log_path = _resolve_log_path()
    if not log_path:
        return
    try:
        # Cap file size at 2 MB by truncating on rotation. Keep one rolled-over
        # copy so a crash doesn't lose the immediately preceding session.
        if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
            backup = log_path.with_suffix(log_path.suffix + ".1")
            try:
                backup.unlink()
            except FileNotFoundError:
                pass
            log_path.rename(backup)
        fh = log_path.open("a", encoding="utf-8", buffering=1)
        sys.stdout = _TeeStream(sys.stdout, fh)
        sys.stderr = _TeeStream(sys.stderr, fh)
    except Exception:
        # Logging setup must never block startup.
        pass


def _configure_logging() -> None:
    from tune_server.utils.error_buffer import capture_processor

    _install_file_logging()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            # Capture warnings/errors into a ring buffer for /diagnostics
            capture_processor,
            structlog.dev.ConsoleRenderer()
            if settings.log_format == "console"
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


class TuneServer:
    """Main application orchestrator."""

    def __init__(self) -> None:
        self._event_bus = EventBus()
        self._db: Database | None = None
        self._scanner: LibraryScanner | None = None
        self._watcher: FileSystemWatcher | None = None
        self._enricher: MetadataEnricher | None = None
        self._zone_manager: ZoneManager | None = None
        self._group_manager: GroupManager | None = None
        self._sync_engine: SyncEngine | None = None
        self._discovery_manager: DiscoveryManager | None = None
        self._http_streamer: HttpAudioStreamer | RustSidecarStreamer | None = None
        self._oh_event_listener = None  # OpenHomeEventListener, shared across outputs
        self._mount_manager = None
        self._ws_manager = None
        self._scan_task: asyncio.Task | None = None
        self._server_ip = settings.advertise_ip or get_local_ip()
        self._api_app = None  # bare FastAPI created in start() (plugins mount here)
        self._serving_app = None  # SPA-wrapped ASGI app passed to uvicorn
        self._plugin_loader = None  # PluginLoader, instantiated in start()

    @property
    def api_app(self):
        """Bare FastAPI app — created in start(). Plugins use this."""
        return self._api_app

    @property
    def serving_app(self):
        """SPA-wrapped ASGI app — what uvicorn serves. None until start() finishes."""
        return self._serving_app

    async def start(self) -> None:
        _configure_logging()
        from tune_server import __version__
        logger.info("tune_server_starting", version=__version__)

        # Startup validation
        from pathlib import Path
        for music_dir in settings.music_dirs:
            if not Path(music_dir).is_dir():
                logger.error("music_dir_not_found", path=music_dir)

        if not check_ffmpeg():
            import platform
            if platform.system() == "Windows":
                hint = "Download from https://ffmpeg.org and place ffmpeg.exe next to tune-server.exe"
            else:
                hint = "Install with: sudo apt install ffmpeg (or brew install ffmpeg on macOS)"
            logger.error("ffmpeg_not_found", hint=hint, configured_path=settings.ffmpeg_path)

        # Rust native acceleration status
        try:
            import tune_native
            from tune_server.library.rust_scanner import rust_scanner_available
            from tune_server.discovery.rust_discovery import rust_discovery_available
            from tune_server.library.metadata_reader import _use_rust_engine
            logger.info(
                "rust_native_engines",
                version=tune_native.version(),
                scanner=rust_scanner_available(),
                discovery=rust_discovery_available(),
                metadata=_use_rust_engine(),
            )
        except ImportError:
            logger.info("rust_native_engines", available=False)

        # Phase 1 — Database + repos
        repos = await self._init_database()

        # Phase 2 — Library scanner
        self._init_scanner()

        # Phase 3 — Zones, groups, sync
        self._init_zones()

        # Phase 4 — HTTP streamer, UPnP, Deezer proxy
        await self._init_http_streamer()

        # Phase 5 — Register output factories + create FastAPI app
        await self._register_output_factories()
        self._api_app = create_api_app()

        # Phase 6 — Plugins
        await self._init_plugins()

        # Phase 7 — Discovery, OpenHome, mounts, SMB
        await self._init_discovery()

        # Phase 8 — WebSocket manager + SPA wrap + zone init
        await self._init_web_and_events()

        # Phase 9 — Streaming auth, deps, schedulers
        await self._init_schedulers(repos)

        # Phase 10 — Initial scan, banner, telemetry
        await self._init_finalize()

    # ------------------------------------------------------------------
    # Private init phases — called from start() in order
    # ------------------------------------------------------------------

    async def _init_database(self) -> dict:
        """Phase 1: Database creation + repository instantiation."""
        self._db = create_database(settings, use_sa=True)
        await self._db.connect()

        from tune_server.db.sa_repository import (
            SAArtistRepo, SAAlbumRepo, SATrackRepo,
            SAPlayQueueRepo, SAZoneRepo, SAPlaylistRepo, SARadioStationRepo,
        )
        track_repo = SATrackRepo(self._db)
        album_repo = SAAlbumRepo(self._db)
        artist_repo = SAArtistRepo(self._db)
        queue_repo = SAPlayQueueRepo(self._db)
        zone_repo = SAZoneRepo(self._db)
        playlist_repo = SAPlaylistRepo(self._db)
        radio_repo = SARadioStationRepo(self._db)
        credit_repo = TrackCreditRepo(self._db)

        return dict(
            track_repo=track_repo, album_repo=album_repo, artist_repo=artist_repo,
            queue_repo=queue_repo, zone_repo=zone_repo, playlist_repo=playlist_repo,
            radio_repo=radio_repo, credit_repo=credit_repo,
        )

    def _init_scanner(self) -> None:
        """Phase 2: LibraryScanner."""
        self._scanner = LibraryScanner(self._db, self._event_bus, credit_repo=TrackCreditRepo(self._db))

    def _init_zones(self) -> None:
        """Phase 3: ZoneManager + GroupManager + SyncEngine."""
        self._zone_manager = ZoneManager(self._db, self._event_bus)
        self._group_manager = GroupManager(self._event_bus)
        self._zone_manager.set_group_manager(self._group_manager)
        self._sync_engine = SyncEngine(self._group_manager)

    async def _init_http_streamer(self) -> None:
        """Phase 4: HttpAudioStreamer (or Rust sidecar) + UPnP MediaServer + Deezer proxy."""
        # Pre-bind a free port so we don't crash when 8080 is taken
        settings.stream_port = pick_free_port(settings.stream_host, settings.stream_port)

        # Try Rust sidecar first (auto/rust), fall back to Python aiohttp
        streamer = create_streamer(
            host=settings.stream_host,
            port=settings.stream_port,
        )
        if isinstance(streamer, RustSidecarStreamer):
            try:
                await streamer.start()
                self._http_streamer = streamer
                logger.info("http_streamer_engine", engine="rust-sidecar", port=settings.stream_port)
            except Exception as e:
                logger.warning("rust_sidecar_start_failed", error=str(e), fallback="python")
                await streamer.stop()
                streamer = HttpAudioStreamer(
                    host=settings.stream_host,
                    port=settings.stream_port,
                )
                self._http_streamer = streamer
        else:
            self._http_streamer = streamer

        # UPnP MediaServer
        self._upnp_server = None
        if settings.upnp_server_enabled:
            from tune_server.upnp_server.server import UpnpMediaServer
            self._upnp_server = UpnpMediaServer(
                server_ip=self._server_ip,
                http_port=settings.stream_port,
                api_port=settings.api_port,
                aiohttp_app=None,
                track_repo=deps.track_repo,
                album_repo=deps.album_repo,
                artist_repo=deps.artist_repo,
                event_bus=self._event_bus,
                friendly_name=settings.upnp_server_name,
            )
            self._http_streamer.on_app_created(self._upnp_server.register_routes)

        # Streaming services need to be created BEFORE http_streamer.start()
        # so the Deezer decrypting proxy can register its route on the
        # streamer's aiohttp app (which is frozen once start() runs).
        self._setup_streaming_services()
        if "deezer" in deps.streaming_services:
            from tune_server.streaming.deezer_proxy import DeezerProxy
            self._deezer_proxy = DeezerProxy(deps.streaming_services["deezer"])
            self._http_streamer.on_app_created(self._deezer_proxy.register_routes)

        # Start the Python aiohttp streamer if it was selected (Rust sidecar
        # is already started above before the fallback check).
        if isinstance(self._http_streamer, HttpAudioStreamer):
            await self._http_streamer.start()
            logger.info("http_streamer_engine", engine="python-aiohttp", port=settings.stream_port)

        # Now that the streamer is up, tell the Deezer service where to
        # build proxy URLs (used by get_stream_url).
        if "deezer" in deps.streaming_services:
            base = f"http://{self._server_ip}:{settings.stream_port}"
            deps.streaming_services["deezer"].set_proxy_base_url(base)

        if self._upnp_server:
            await self._upnp_server.start()

    async def _init_plugins(self) -> None:
        """Phase 6: PluginLoader + PluginContext + discover_and_setup."""
        # Auto-install plugins from env var (Docker persistence)
        if settings.install_plugins:
            from tune_server.plugins.store import auto_install_plugins
            await auto_install_plugins(settings.install_plugins)

        # Plugin Store manager (fetches catalog from mozaiklabs.fr)
        from tune_server.plugins.store import PluginStoreManager
        self._store_manager = PluginStoreManager()
        deps.store_manager = self._store_manager

        # Plugin discovery + setup. Each installed plugin (entry_point group
        # ``tune_server.plugins``) gets a chance to register output types,
        # router(s), event subscribers, and player hooks BEFORE zones spawn.
        from tune_server.plugins import PluginContext, PluginLoader
        self._plugin_loader = PluginLoader()
        plugin_ctx = PluginContext(
            event_bus=self._event_bus,
            api_app=self._api_app,
            db=self._db,
            settings=settings,
            _zone_manager=self._zone_manager,
        )
        await self._plugin_loader.discover_and_setup(plugin_ctx)

        # Expose plugin loader to API routes via deps
        deps.plugin_loader = self._plugin_loader

        # Wire plugin-contributed Player hooks. ZoneManager applies them to
        # every Player it creates (existing + future).
        if self._plugin_loader.pending_player_hooks:
            self._zone_manager.set_player_hooks(self._plugin_loader.pending_player_hooks)

        # Now that plugins have registered their routers, wrap the FastAPI
        # app with the SPA fallback middleware (if a web bundle exists).
        # The wrapped object is what uvicorn serves; it must be the outermost
        # layer so static SPA routing works.
        self._serving_app = wrap_for_serving(self._api_app)

    async def _init_discovery(self) -> None:
        """Phase 7: DiscoveryManager + OpenHome events + mounts + SMB."""
        # Discovery — start BEFORE zone init so DLNA devices can be found
        self._discovery_manager = DiscoveryManager(self._event_bus)
        await self._discovery_manager.start()

        # OpenHome event listener — shared receiver for UPnP NOTIFY callbacks
        try:
            from tune_server.outputs.oh_events import OpenHomeEventListener
            self._oh_event_listener = OpenHomeEventListener(self._server_ip)
            await self._oh_event_listener.start()
        except Exception:
            logger.warning("oh_event_listener_start_failed", exc_info=True)
            self._oh_event_listener = None

        # Mount manager for network shares
        if settings.network_shares_enabled or settings.network_media_servers_enabled:
            from tune_server.network.mount_manager import MountManager
            self._mount_manager = MountManager(
                self._db, self._event_bus, self._scanner, settings.smb_mount_dir,
            )
            await self._mount_manager.initialize()
            deps.mount_manager = self._mount_manager

        # SMB auto-discovery (active network scan)
        self._smb_discovery = None
        if settings.smb_auto_discovery:
            from tune_server.network.smb_discovery import SmbAutoDiscovery
            self._smb_discovery = SmbAutoDiscovery(
                self._event_bus,
                scan_interval=settings.smb_scan_interval,
            )
            await self._smb_discovery.start()
            deps.smb_discovery = self._smb_discovery

    async def _init_web_and_events(self) -> None:
        """Phase 8: WebSocket manager + zone initialization."""
        # WebSocket manager — start BEFORE zones so events are never lost
        self._ws_manager = await setup_websocket_manager(self._event_bus)

        # Brief wait for initial SSDP scan to find devices
        await asyncio.sleep(2)

        # Initialize zones from DB (now devices should be available)
        await self._zone_manager.initialize()

        # Retry unavailable zones with progressive delays
        async def _retry_zones():
            for delay in [15, 30, 60]:
                await asyncio.sleep(delay)
                await self._zone_manager.retry_pending_zones()
                if not self._zone_manager._pending_zones:
                    break
        asyncio.create_task(_retry_zones())

    async def _init_schedulers(self, repos: dict) -> None:
        """Phase 9: Streaming auth, deps wiring, schedulers, background services."""
        # Restore streaming auth now that DB is available
        await self._restore_streaming_auth()
        self._build_stream_url_resolver()

        # Populate deps for API
        deps.db = self._db
        deps.event_bus = self._event_bus
        deps.scanner = self._scanner
        deps.zone_manager = self._zone_manager
        deps.group_manager = self._group_manager
        deps.discovery_manager = self._discovery_manager
        deps.track_repo = repos["track_repo"]
        deps.album_repo = repos["album_repo"]
        deps.artist_repo = repos["artist_repo"]
        deps.playlist_repo = repos["playlist_repo"]
        deps.queue_repo = repos["queue_repo"]
        deps.zone_repo = repos["zone_repo"]
        deps.radio_repo = repos["radio_repo"]
        deps.credit_repo = repos["credit_repo"]
        from tune_server.db.sa_repository import PlaybackHistoryRepo
        history_repo = PlaybackHistoryRepo(self._db)
        deps.history_repo = history_repo
        self._setup_playback_history(history_repo)
        self._setup_auto_resume()
        from tune_server.db.sa_repository import SARadioFavoriteRepo, SAPartyVoteRepo, SAAlbumRatingRepo
        deps.radio_fav_repo = SARadioFavoriteRepo(self._db)
        deps.party_vote_repo = SAPartyVoteRepo(self._db)
        deps.album_rating_repo = SAAlbumRatingRepo(self._db)

        # Auto-update checker
        from tune_server.updater import UpdateChecker
        self._update_checker = UpdateChecker(event_bus=self._event_bus)
        deps.update_checker = self._update_checker
        self._update_checker.start()

        # Spotify Connect receiver (zeroconf-based, optional)
        from tune_server.spotify_connect import SpotifyConnectManager
        self._spotify_connect = SpotifyConnectManager(
            self._event_bus, zone_manager=self._zone_manager,
        )
        deps.spotify_connect = self._spotify_connect

        # v0.8.0 — seed default Smart Collections on first start. Idempotent
        # (skip names that already exist), so it's safe to call on every
        # boot. Users who delete a default keep it deleted across restarts.
        try:
            from tune_server.library.smart_collection import (
                SmartCollectionRepo, seed_default_collections,
            )
            inserted = await seed_default_collections(SmartCollectionRepo(self._db))
            if inserted > 0:
                logger.info("smart_collections_seeded", count=inserted)
        except Exception:
            logger.exception("smart_collections_seed_failed")

        try:
            from tune_server.db.sa_repository import (
                SmartPlaylistRepo, seed_default_smart_playlists,
            )
            inserted = await seed_default_smart_playlists(SmartPlaylistRepo(self._db))
            if inserted > 0:
                logger.info("smart_playlists_seeded", count=inserted)
        except Exception:
            logger.exception("smart_playlists_seed_failed")

        # Seed default Admin profile on first start (idempotent)
        try:
            from tune_server.api.routes.profiles import seed_default_admin_profile
            await seed_default_admin_profile()
        except Exception:
            logger.exception("admin_profile_seed_failed")

        if settings.spotify_connect_enabled and settings.spotify_connect_zone_id is not None:
            try:
                await self._spotify_connect.enable(
                    zone_id=settings.spotify_connect_zone_id,
                    device_name=settings.spotify_connect_device_name,
                )
            except FileNotFoundError as exc:
                logger.warning("spotify_connect_unavailable", error=str(exc))

        # Playlist auto-sync scheduler
        from tune_server.playlist_manager.scheduler import AutoSyncScheduler
        self._autosync = AutoSyncScheduler(
            db=self._db,
            streaming_manager=deps.streaming_manager,
        )
        self._autosync.start()

        # Start sync engine
        await self._sync_engine.start()

        # Filesystem watcher
        if settings.watch_filesystem:
            self._watcher = FileSystemWatcher(
                settings.music_dirs, self._scanner, self._db, self._event_bus
            )
            await self._watcher.start()
            deps.watcher = self._watcher

        # Metadata enricher
        self._enricher = MetadataEnricher(self._db)
        await self._enricher.start()
        deps.enricher = self._enricher

        # Artist metadata enrichment client (mozaiklabs.fr)
        if settings.artist_metadata_enabled:
            from tune_server.metadata.artist_enrichment import ArtistEnrichmentClient
            self._artist_enrichment = ArtistEnrichmentClient()
            deps.artist_enrichment = self._artist_enrichment
            logger.info("artist_enrichment_enabled", url=settings.artist_metadata_url)

        # Auto-enrich after library scan
        if settings.enrich_on_scan:
            async def _on_scan_complete(event: Event) -> None:
                logger.info("auto_enrich_after_scan")
                await self._enricher.enrich_now()
            self._event_bus.on(EventType.LIBRARY_SCAN_COMPLETED, _on_scan_complete)

        # Alarm scheduler
        from tune_server.alarms import AlarmScheduler
        self._alarm_scheduler = AlarmScheduler(self._db, self._trigger_alarm)
        await self._alarm_scheduler.start()
        deps.alarm_scheduler = self._alarm_scheduler

        # Scan scheduler (daily scheduled scan)
        from tune_server.library.scan_scheduler import ScanScheduler
        scan_time = getattr(settings, "scan_schedule", None)
        self._scan_scheduler = ScanScheduler(
            db=self._db,
            scanner=self._scanner,
            music_dirs=settings.music_dirs,
            initial_time=scan_time,
        )
        await self._scan_scheduler.start()
        deps.scan_scheduler = self._scan_scheduler

        # Health monitor (background resource & playback checks)
        from tune_server.utils.health_monitor import HealthMonitor
        self._health_monitor = HealthMonitor(self._event_bus)
        await self._health_monitor.start()
        deps.health_monitor = self._health_monitor

        # Desktop notifications for track changes (opt-in via TUNE_NOTIFICATIONS_ENABLED)
        from tune_server.notifications import setup_notifications
        setup_notifications(self._event_bus, self._server_ip, settings.api_port)

        # DLNA adaptive buffer: periodic stability check (decrease buffers for stable devices)
        self._dlna_buffer_check_task = asyncio.create_task(self._dlna_buffer_stability_loop())

    async def _init_finalize(self) -> None:
        """Phase 10: Initial scan, startup banner, telemetry."""
        from tune_server import __version__

        # Initial scan
        if settings.scan_on_startup:
            self._scan_task = asyncio.create_task(self._scanner.scan(settings.music_dirs))

        await self._event_bus.emit(Event(
            type=EventType.SYSTEM_STARTED,
            source="app",
        ))

        logger.info(
            "tune_server_started",
            api_url=f"http://{self._server_ip}:{settings.api_port}",
            stream_url=f"http://{self._server_ip}:{settings.stream_port}",
        )

        # Print a clear startup banner visible in the console / .bat window.
        # Non-technical testers need to see the URL prominently.
        print()
        print("=" * 60)
        print(f"  Tune Server v{__version__} is running")
        print(f"  Web UI:  http://localhost:{settings.api_port}")
        print(f"  Network: http://{self._server_ip}:{settings.api_port}")
        if not settings.music_dirs or not any(
            __import__('pathlib').Path(d).is_dir() for d in settings.music_dirs
        ):
            print()
            print("  NOTE: No music directory found.")
            print("  Add one via the web UI (Settings) or set TUNE_MUSIC_DIRS")
            print("  in your .env file.")
        if not check_ffmpeg():
            print()
            print("  WARNING: FFmpeg not found -- transcoding disabled.")
        print("=" * 60)
        print()

        asyncio.create_task(self._report_install_or_update())

    async def _dlna_buffer_stability_loop(self) -> None:
        """Periodically check if stable DLNA devices can have their buffer reduced."""
        from tune_server.outputs.dlna_buffer_stats import dlna_buffer_registry
        try:
            while True:
                await asyncio.sleep(300)  # every 5 minutes
                dlna_buffer_registry.check_all_stability()
        except asyncio.CancelledError:
            pass

    async def _report_install_or_update(self) -> None:
        """Report install or update to mozaiklabs.fr analytics (fire-and-forget).

        Sends a lightweight anonymous payload with version, platform, arch,
        Python version, library size and zone count so we can understand the
        install base. Non-blocking, never crashes the server.
        """
        try:
            import platform as _platform
            from pathlib import Path
            from tune_server import __version__

            os_map = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}
            plat = os_map.get(_platform.system(), _platform.system().lower())

            version_file = Path(settings.db_path).parent / ".last_version"
            previous = None
            if version_file.exists():
                previous = version_file.read_text().strip()

            if previous == __version__:
                return

            track_type = "update" if previous else "install"
            version_file.write_text(__version__)

            # Gather lightweight telemetry fields
            track_count = 0
            zone_count = 0
            try:
                if deps.track_repo:
                    track_count = await deps.track_repo.count()
            except Exception:
                pass
            try:
                if deps.zone_manager:
                    zone_count = len(deps.zone_manager.list_zones())
            except Exception:
                pass

            db_engine = "postgres" if getattr(settings, "db_engine", "") == "postgres" else "sqlite"

            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                await session.post("https://mozaiklabs.fr/api/v1/installs/track", json={
                    "version": __version__,
                    "platform": plat,
                    "type": track_type,
                    "previous_version": previous,
                    "arch": _platform.machine(),
                    "python": _platform.python_version(),
                    "tracks": track_count,
                    "zones": zone_count,
                    "db_engine": db_engine,
                })
            logger.info("install_tracked", type=track_type, version=__version__, previous=previous)
        except Exception:
            logger.debug("install_track_failed", exc_info=True)

    async def _register_output_factories(self) -> None:
        from tune_server.models import OutputType
        from tune_server.outputs.dlna import DlnaOutput
        from tune_server.outputs.airplay import AirPlayOutput
        from tune_server.outputs.bluos import BluosOutput
        from tune_server.outputs.chromecast import ChromecastOutput
        from tune_server.outputs.local import LocalOutput
        from tune_server.outputs.squeezebox import SqueezeboxOutput

        async def create_dlna_output(device_id: str | None):
            if not device_id or not self._discovery_manager or not self._discovery_manager.ssdp:
                logger.warning("dlna_factory_no_discovery", device_id=device_id)
                return None
            dmr = self._discovery_manager.ssdp.get_dmr_device(device_id)
            if not dmr:
                # Fallback: match by IP address if device_id looks like an IP
                if "." in device_id and "uuid:" not in device_id:
                    for did, dev in self._discovery_manager.ssdp.devices.items():
                        if dev.host == device_id and dev.type == "dlna":
                            dmr = self._discovery_manager.ssdp.get_dmr_device(did)
                            if dmr:
                                logger.info("dlna_factory_matched_by_ip", ip=device_id, uuid=did)
                                # Update DB with correct UUID for future boots
                                try:
                                    zone_rows = await self._zone_manager._zone_repo.list()
                                    for zr in zone_rows:
                                        if zr.get("output_device_id") == device_id:
                                            await self._zone_manager._zone_repo.update(
                                                zr["id"], output_device_id=did
                                            )
                                            logger.info("zone_device_id_corrected", zone_id=zr["id"], old=device_id, new=did)
                                except Exception:
                                    pass
                                break
            if not dmr:
                # Device visible but no DMR — try a live MinimalDmrDevice probe
                disc = self._discovery_manager.ssdp.devices.get(device_id)
                if disc and disc.host:
                    from tune_server.outputs.minimal_dmr import MinimalDmrDevice
                    port = disc.port or 80
                    base_url = f"http://{disc.host}:{port}"
                    desc_url = disc.capabilities.get("description_url") if disc.capabilities else None
                    minimal = MinimalDmrDevice(
                        name=disc.name, base_url=base_url, description_url=desc_url,
                    )
                    try:
                        if await minimal.probe():
                            async with self._discovery_manager.ssdp._lock:
                                self._discovery_manager.ssdp._dmr_devices[device_id] = minimal
                                if disc.name != minimal.name:
                                    disc.name = minimal.name
                                    disc.capabilities["device_name"] = minimal.name
                            dmr = minimal
                            logger.info("dlna_factory_live_probe_ok", device_id=device_id, name=minimal.name)
                    except Exception as e:
                        logger.debug("dlna_factory_live_probe_failed", device_id=device_id, error=str(e))
            if not dmr:
                logger.warning("dlna_factory_device_not_found", device_id=device_id,
                             available=list(self._discovery_manager.ssdp._dmr_devices.keys()))
                return None
            # Pass device info for DSD detection
            disc_device = self._discovery_manager.ssdp.devices.get(device_id)
            caps = disc_device.capabilities if disc_device else {}
            device_ip = disc_device.host if disc_device else None
            return DlnaOutput(
                dmr, self._http_streamer, self._server_ip,
                sink_protocols=caps.get("sink_protocols", []),
                device_name=caps.get("device_name", ""),
                device_model=caps.get("model", ""),
                device_ip=device_ip,
                device_id=device_id,
                device_manufacturer=caps.get("manufacturer", ""),
            )

        async def create_airplay_output(device_id: str | None):
            if not device_id:
                raise RuntimeError("AirPlay: no device_id specified")
            if not self._discovery_manager or not self._discovery_manager.mdns:
                raise RuntimeError(
                    "AirPlay: mDNS discovery is not running. "
                    "On Windows, install Apple Bonjour (included with iTunes) "
                    "and check that your firewall allows mDNS (UDP 5353)."
                )
            config = self._discovery_manager.mdns.get_atv_config(device_id)
            if not config:
                known = list(self._discovery_manager.mdns.devices.keys())
                raise RuntimeError(
                    f"AirPlay: device '{device_id}' not found via mDNS. "
                    f"Discovered devices: {known or 'none'}. "
                    "Check that the device is on and on the same network."
                )
            try:
                import pyatv

                # Load saved credentials from DB
                if self._db:
                    cred_row = await self._db.fetchone(
                        "SELECT credentials FROM device_credentials WHERE device_id = ?",
                        (device_id,),
                    )
                    if cred_row and cred_row[0]:
                        creds = cred_row[0]
                        for protocol in [pyatv.Protocol.AirPlay, pyatv.Protocol.RAOP, pyatv.Protocol.Companion]:
                            if config.get_service(protocol) is not None:
                                config.set_credentials(protocol, creds)
                        logger.info("airplay_credentials_loaded", device_id=device_id)

                loop = asyncio.get_running_loop()
                try:
                    atv = await pyatv.connect(config, loop)
                except Exception as first_err:
                    logger.warning("airplay_connect_retry_no_auth",
                                   device_id=device_id, first_error=str(first_err))
                    for protocol in [pyatv.Protocol.AirPlay, pyatv.Protocol.RAOP, pyatv.Protocol.Companion]:
                        svc = config.get_service(protocol)
                        if svc is not None:
                            svc.credentials = None
                            svc.password = None
                    atv = await pyatv.connect(config, loop)
                    logger.info("airplay_connected_without_auth", device_id=device_id)
                device = self._discovery_manager.get_device(device_id)
                name = device.name if device else "AirPlay"
                return AirPlayOutput(atv, device_name=name,
                                     streamer=self._http_streamer, server_ip=self._server_ip)
            except ImportError:
                raise RuntimeError("AirPlay: pyatv library is not installed")
            except RuntimeError:
                raise
            except Exception as exc:
                logger.exception("airplay_connect_error", device_id=device_id)
                raise RuntimeError(f"AirPlay: connection failed — {exc}") from exc

        async def create_local_output(device_id: str | None):
            return LocalOutput(device_name=device_id)

        async def create_chromecast_output(device_id: str | None):
            if not device_id:
                raise RuntimeError("Chromecast: no device_id specified")
            if not self._discovery_manager or not self._discovery_manager.cast:
                raise RuntimeError("Chromecast: Cast discovery is not running")
            resolved_id = self._discovery_manager.cast.resolve_device_id(device_id)
            cast = await self._discovery_manager.cast.reconnect_cast_device(device_id)
            if not cast:
                known = list(self._discovery_manager.cast.devices.keys())
                raise RuntimeError(
                    f"Chromecast: device '{device_id}' not found. "
                    f"Discovered: {known or 'none'}."
                )
            device = self._discovery_manager.get_device(resolved_id or device_id)
            name = device.name if device else "Chromecast"
            return ChromecastOutput(cast, self._http_streamer, self._server_ip, device_name=name)

        async def create_bluos_output(device_id: str | None):
            if not device_id:
                raise RuntimeError("BluOS: no device_id specified")
            if not self._discovery_manager or not self._discovery_manager.bluos:
                raise RuntimeError("BluOS: BluOS discovery is not running")
            host_port = self._discovery_manager.bluos.get_bluos_host(device_id)
            if not host_port:
                known = list(self._discovery_manager.bluos.devices.keys())
                raise RuntimeError(
                    f"BluOS: device '{device_id}' not found. "
                    f"Discovered: {known or 'none'}."
                )
            host, port = host_port
            device = self._discovery_manager.get_device(device_id)
            name = device.name if device else "BluOS"
            return BluosOutput(host, self._http_streamer, self._server_ip, port=port, device_name=name)

        async def create_openhome_output(device_id: str | None):
            if not device_id or not self._discovery_manager or not self._discovery_manager.openhome:
                return None
            oh = self._discovery_manager.openhome
            disc = oh.devices.get(device_id)
            urls = oh.get_service_urls(device_id)
            if not disc or not urls:
                logger.warning("openhome_factory_not_found", device_id=device_id)
                return None
            from tune_server.outputs.openhome import OpenHomeOutput
            return OpenHomeOutput(
                device_name=disc.name,
                service_urls=urls,
                server_ip=self._server_ip,
                streamer=self._http_streamer,
                base_url=disc.capabilities.get("base_url", ""),
                event_listener=self._oh_event_listener,
                event_sub_urls=oh.get_event_sub_urls(device_id),
            )

        async def create_squeezebox_output(device_id: str | None):
            if not device_id:
                raise RuntimeError("Squeezebox: no device_id specified")
            if not self._discovery_manager or not self._discovery_manager.squeezebox:
                raise RuntimeError("Squeezebox: Squeezebox discovery is not running")
            info = self._discovery_manager.squeezebox.get_lms_for_player(device_id)
            if not info:
                known = list(self._discovery_manager.squeezebox.devices.keys())
                raise RuntimeError(
                    f"Squeezebox: device '{device_id}' not found. "
                    f"Discovered: {known or 'none'}."
                )
            lms_host, lms_port, player_mac = info
            device = self._discovery_manager.get_device(device_id)
            name = device.name if device else "Squeezebox"
            return SqueezeboxOutput(
                lms_host, player_mac, self._http_streamer, self._server_ip,
                lms_port=lms_port, device_name=name,
            )

        self._zone_manager.register_output_factory(OutputType.DLNA, create_dlna_output)
        self._zone_manager.register_output_factory(OutputType.AIRPLAY, create_airplay_output)
        self._zone_manager.register_output_factory(OutputType.CHROMECAST, create_chromecast_output)
        self._zone_manager.register_output_factory(OutputType.BLUOS, create_bluos_output)
        self._zone_manager.register_output_factory(OutputType.LOCAL, create_local_output)
        self._zone_manager.register_output_factory(OutputType.OPENHOME, create_openhome_output)
        self._zone_manager.register_output_factory(OutputType.SQUEEZEBOX, create_squeezebox_output)

    def _setup_auto_resume(self) -> None:
        """Save playback state on events and auto-resume on startup."""
        zm = self._zone_manager

        async def _save_state(event: Event):
            data = event.data or {}
            zone_id = data.get("zone_id")
            if not zone_id or not zm:
                return
            zone = zm.get_zone(zone_id)
            if zone:
                is_playing = zone.player.state.value == "playing"
                pos = zone.player.position_ms
                await zone.save_playback_state(is_playing, pos)

        async def _save_stopped(event: Event):
            data = event.data or {}
            zone_id = data.get("zone_id")
            if not zone_id or not zm:
                return
            zone = zm.get_zone(zone_id)
            if zone:
                await zone.save_playback_state(False, 0)

        self._event_bus.on(EventType.PLAYBACK_STARTED, _save_state)
        self._event_bus.on(EventType.PLAYBACK_TRACK_CHANGED, _save_state)
        self._event_bus.on(EventType.PLAYBACK_STOPPED, _save_stopped)

        async def _auto_resume():
            await asyncio.sleep(5)
            if not zm:
                return
            for zone in zm.list_zones():
                try:
                    row = await self._db.fetchone(
                        "SELECT was_playing, last_position_ms FROM zones WHERE id = ?",
                        (zone.zone_id,),
                    )
                    if row and row.get("was_playing"):
                        pos = row.get("last_position_ms", 0) or 0
                        logger.info("auto_resume_playback", zone_id=zone.zone_id,
                                    zone_name=zone.name, position_ms=pos)
                        await zone.player.play(seek_ms=pos)
                except Exception:
                    logger.debug("auto_resume_failed", zone_id=zone.zone_id, exc_info=True)

        asyncio.create_task(_auto_resume())

    def _setup_playback_history(self, history_repo) -> None:
        """Record each played track in playback_history via EventBus.

        Tracks elapsed wall-clock time per zone so that radio streams
        (which have no duration_ms) get accurate listened_ms values.
        """
        _zone_play_start: dict[str, tuple] = {}

        async def _flush_previous(zone_id: str):
            prev = _zone_play_start.pop(zone_id, None)
            if not prev:
                return
            track, cover, start_mono = prev
            elapsed_ms = int((time.monotonic() - start_mono) * 1000)
            if elapsed_ms < 2000:
                return
            try:
                await history_repo.record(
                    track_id=track.id,
                    zone_id=zone_id,
                    track_title=track.title,
                    artist_name=track.artist_name,
                    album_title=track.album_title,
                    cover_path=cover,
                    duration_ms=track.duration_ms,
                    listened_ms=elapsed_ms,
                    source=track.source.value if track.source else None,
                )
            except Exception:
                logger.debug("playback_history_record_error", exc_info=True)

        async def _on_track_changed(event: Event):
            data = event.data or {}
            zone_id = data.get("zone_id")
            if zone_id:
                await _flush_previous(zone_id)
            zone = self._zone_manager.get_zone(zone_id) if zone_id else None
            track = zone.player._queue.current if zone else None
            if track and zone_id:
                cover = track.cover_path
                if not cover and track.album_id and deps.album_repo:
                    try:
                        album = await deps.album_repo.get(track.album_id)
                        if album:
                            cover = album.cover_path
                    except Exception:
                        pass
                _zone_play_start[zone_id] = (track, cover, time.monotonic())

        async def _on_stopped(event: Event):
            data = event.data or {}
            zone_id = data.get("zone_id")
            if zone_id:
                await _flush_previous(zone_id)

        self._event_bus.on(EventType.PLAYBACK_TRACK_CHANGED, _on_track_changed)
        self._event_bus.on(EventType.PLAYBACK_STARTED, _on_track_changed)
        self._event_bus.on(EventType.PLAYBACK_STOPPED, _on_stopped)

        # Last.fm scrobbling
        self._setup_lastfm_scrobbling()

    def _setup_lastfm_scrobbling(self) -> None:
        """Wire Last.fm scrobbling to the event bus.

        Credentials are loaded from the DB first (set via the web UI), with a
        fallback to env-var settings for backwards compatibility.

        Scrobble rules (per Last.fm spec):
        - Send "now playing" when a track starts.
        - Scrobble after the track has been listened to for >= 50% of its
          duration or 4 minutes, whichever comes first.
        - Tracks shorter than 30 seconds are not scrobbled.
        - Skipped tracks (insufficient play time) are not scrobbled.
        """
        import json as _json
        from tune_server.metadata.lastfm_scrobbler import LastfmScrobbler, should_scrobble

        # Mutable state shared across closures — keyed by zone_id.
        # Each entry: {"track": <track obj>, "started_at": <epoch>, "start_mono": <monotonic>}
        _playing: dict[str, dict] = {}
        _scrobbler_ref: list[LastfmScrobbler | None] = [None]
        _scrobble_enabled_ref: list[bool] = [False]

        async def _load_scrobbler() -> LastfmScrobbler | None:
            """Load or refresh the scrobbler from DB-stored or env credentials."""
            api_key = ""
            api_secret = ""
            session_key = ""
            scrobble_enabled = False

            # Try DB first (web UI configuration)
            try:
                row = await deps.db.fetchone(
                    "SELECT token_data FROM streaming_auth WHERE service = ?",
                    ("lastfm",),
                )
                if row:
                    payload = _json.loads(row["token_data"])
                    api_key = (payload.get("api_key") or "").strip()
                    api_secret = (payload.get("api_secret") or "").strip()
                    session_key = (payload.get("session_key") or "").strip()
                    scrobble_enabled = bool(payload.get("scrobble_enabled"))
            except Exception:
                logger.debug("lastfm_db_load_error", exc_info=True)

            # Fallback to env vars
            if not api_key:
                api_key = getattr(settings, "lastfm_api_key", "") or ""
            if not api_secret:
                api_secret = getattr(settings, "lastfm_api_secret", "") or ""
            if not session_key:
                session_key = getattr(settings, "lastfm_session_key", "") or ""
            if not scrobble_enabled:
                scrobble_enabled = getattr(settings, "lastfm_scrobble_enabled", False)

            _scrobble_enabled_ref[0] = scrobble_enabled
            if not api_key or not api_secret or not session_key or not scrobble_enabled:
                _scrobbler_ref[0] = None
                return None

            scrobbler = _scrobbler_ref[0]
            if scrobbler is None:
                scrobbler = LastfmScrobbler(
                    api_key=api_key,
                    api_secret=api_secret,
                    session_key=session_key,
                )
                _scrobbler_ref[0] = scrobbler
                logger.info("lastfm_scrobbling_enabled")
            else:
                # Refresh session key in case it changed (re-auth via UI)
                scrobbler.set_session_key(session_key)
            return scrobbler

        async def _maybe_scrobble_previous(zone_id: str) -> None:
            """Scrobble the previously-playing track for this zone if eligible."""
            prev = _playing.pop(zone_id, None)
            if not prev:
                return

            scrobbler = await _load_scrobbler()
            if not scrobbler or not scrobbler.is_authenticated:
                return

            track = prev["track"]
            if not track or not track.artist_name or not track.title:
                return

            listened_ms = int((time.monotonic() - prev["start_mono"]) * 1000)
            if not should_scrobble(track.duration_ms, listened_ms):
                logger.debug("lastfm_scrobble_skip_insufficient",
                             track=track.title, listened_ms=listened_ms,
                             duration_ms=track.duration_ms)
                return

            await scrobbler.scrobble(
                artist=track.artist_name,
                track=track.title,
                album=track.album_title,
                duration=track.duration_ms,
                timestamp=int(prev["started_at"]),
            )

        async def _on_playback_started(event: Event) -> None:
            """Track started — send 'now playing' and record start time."""
            data = event.data or {}
            zone_id = data.get("zone_id")
            if not zone_id:
                return

            zone = self._zone_manager.get_zone(zone_id)
            track = zone.player._queue.current if zone else None
            if not track:
                return

            _playing[zone_id] = {
                "track": track,
                "started_at": time.time(),
                "start_mono": time.monotonic(),
            }

            # Send "now playing" to Last.fm
            scrobbler = await _load_scrobbler()
            if scrobbler and scrobbler.is_authenticated and track.artist_name and track.title:
                await scrobbler.update_now_playing(
                    artist=track.artist_name,
                    track=track.title,
                    album=track.album_title,
                    duration=track.duration_ms,
                )

        async def _on_track_changed(event: Event) -> None:
            """Previous track ended (or was skipped) — scrobble if eligible,
            then record the new track and send 'now playing'."""
            data = event.data or {}
            zone_id = data.get("zone_id")
            if not zone_id:
                return

            # Scrobble the previous track (checks listen duration)
            await _maybe_scrobble_previous(zone_id)

            # Record the new track that just started
            zone = self._zone_manager.get_zone(zone_id)
            track = zone.player._queue.current if zone else None
            if not track:
                return

            _playing[zone_id] = {
                "track": track,
                "started_at": time.time(),
                "start_mono": time.monotonic(),
            }

            scrobbler = await _load_scrobbler()
            if scrobbler and scrobbler.is_authenticated and track.artist_name and track.title:
                await scrobbler.update_now_playing(
                    artist=track.artist_name,
                    track=track.title,
                    album=track.album_title,
                    duration=track.duration_ms,
                )

        async def _on_playback_stopped(event: Event) -> None:
            """Playback stopped — scrobble the last track if eligible."""
            data = event.data or {}
            zone_id = data.get("zone_id")
            if zone_id:
                await _maybe_scrobble_previous(zone_id)

        self._event_bus.on(EventType.PLAYBACK_STARTED, _on_playback_started)
        self._event_bus.on(EventType.PLAYBACK_TRACK_CHANGED, _on_track_changed)
        self._event_bus.on(EventType.PLAYBACK_STOPPED, _on_playback_stopped)

        # Initial load to log status at startup
        asyncio.create_task(_load_scrobbler())

    def _setup_streaming_services(self) -> None:
        if settings.tidal_enabled:
            from tune_server.streaming.tidal import TidalService
            deps.streaming_services["tidal"] = TidalService()
            logger.info("tidal_service_enabled")

        if settings.qobuz_enabled:
            from tune_server.streaming.qobuz import QobuzService
            deps.streaming_services["qobuz"] = QobuzService()
            logger.info("qobuz_service_enabled")

        if settings.youtube_enabled:
            from tune_server.streaming.youtube import YouTubeService
            deps.streaming_services["youtube"] = YouTubeService()
            logger.info("youtube_service_enabled")

        if settings.amazon_music_enabled:
            from tune_server.streaming.amazon import AmazonMusicService
            deps.streaming_services["amazon"] = AmazonMusicService()
            logger.info("amazon_service_enabled")

        if settings.spotify_enabled:
            from tune_server.streaming.spotify import SpotifyService
            deps.streaming_services["spotify"] = SpotifyService()
            logger.info("spotify_service_enabled")

        if settings.deezer_enabled:
            from tune_server.streaming.deezer import DeezerService
            deps.streaming_services["deezer"] = DeezerService(
                arl=settings.deezer_arl,
                quality=settings.deezer_quality,
            )
            logger.info("deezer_service_enabled", quality=settings.deezer_quality)

    async def _restore_streaming_auth(self) -> None:
        """Restore streaming service auth tokens from DB."""
        for name, service in list(deps.streaming_services.items()):
            if self._db:
                try:
                    if await service.restore_auth(self._db):
                        logger.info("streaming_session_restored", service=name)
                except Exception:
                    logger.exception("streaming_restore_error", service=name)

    def _build_stream_url_resolver(self) -> None:
        """Build and wire the stream URL resolver for playback of streaming tracks."""
        from tune_server.models import Track

        async def _resolve_stream_url(track: Track) -> str | None:
            service = deps.streaming_services.get(track.source.value)
            if service and service.is_authenticated:
                return await service.get_stream_url(track.source_id)
            return None

        deps.stream_url_resolver = _resolve_stream_url

        # Set resolver on all existing zones
        if self._zone_manager:
            for zone in self._zone_manager.list_zones():
                zone.player.set_stream_url_resolver(_resolve_stream_url)
            self._zone_manager.set_stream_url_resolver(_resolve_stream_url)

    async def _safe_stop(self, name: str, coro) -> None:
        try:
            await asyncio.wait_for(coro, timeout=COMPONENT_SHUTDOWN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error("component_shutdown_timeout", component=name)
        except asyncio.CancelledError:
            # Intentionally suppressed: during shutdown we want to continue
            # cleaning up remaining components even if one gets cancelled.
            logger.warning("component_shutdown_cancelled", component=name)
        except Exception:
            logger.exception("component_shutdown_error", component=name)

    async def _trigger_alarm(self, zone_id, source_type, source_id, volume=50, fade_in=60):
        """Called by AlarmScheduler when an alarm fires.

        Starts playback at volume 0 then fades in to the target volume
        over *fade_in* seconds.
        """
        from tune_server.alarms import fade_in_volume
        from tune_server.db.sa_repository import _row_to_track
        from tune_server.models import Source, Track

        zone = self._zone_manager.get_zone(zone_id) if zone_id else None
        if not zone:
            zones = self._zone_manager.list_zones()
            zone = zones[0] if zones else None
        if not zone:
            logger.warning("alarm_no_zone")
            return

        target_vol = max(0.0, min(1.0, volume / 100.0))
        tracks: list[Track] = []

        if source_type == "radio":
            row = await self._db.fetchone(
                "SELECT * FROM radio_stations WHERE id = ?", (int(source_id),)
            )
            if row:
                tracks = [Track(
                    title=row["name"],
                    file_path=row["stream_url"],
                    source=Source.RADIO,
                    cover_path=row.get("logo_url"),
                )]
            else:
                logger.warning("alarm_radio_not_found", source_id=source_id)
                return

        elif source_type == "playlist":
            rows = await self._db.fetchall(
                "SELECT t.* FROM tracks t JOIN playlist_tracks pt ON pt.track_id = t.id "
                "WHERE pt.playlist_id = ? ORDER BY pt.position",
                (int(source_id),),
            )
            tracks = [_row_to_track(r) for r in rows]

        elif source_type == "album":
            rows = await self._db.fetchall(
                "SELECT * FROM tracks WHERE album_id = ? ORDER BY disc_number, track_number",
                (int(source_id),),
            )
            tracks = [_row_to_track(r) for r in rows]

        elif source_type == "artist":
            rows = await self._db.fetchall(
                "SELECT * FROM tracks WHERE artist_id = ? ORDER BY RANDOM() LIMIT 50",
                (int(source_id),),
            )
            tracks = [_row_to_track(r) for r in rows]

        if not tracks:
            logger.warning("alarm_no_tracks", source_type=source_type, source_id=source_id)
            return

        # Start at volume 0, begin playback, then fade in
        await zone.player.set_volume(0.0)
        await zone.player.play(tracks=tracks)
        logger.info("alarm_playback_started", zone=zone.name, source_type=source_type,
                     source_id=source_id, target_volume=target_vol, fade_in=fade_in)

        # Fade in volume in background
        await fade_in_volume(zone.player.set_volume, target_vol, fade_in)

    async def stop(self) -> None:
        logger.info("tune_server_stopping")

        await self._event_bus.emit(Event(
            type=EventType.SYSTEM_STOPPING,
            source="app",
        ))

        # Tear down plugins first (their async resources may depend on the
        # core: event_bus, db, etc. — drain them before tearing down core).
        if self._plugin_loader:
            await self._safe_stop("plugins", self._plugin_loader.teardown_all())

        if hasattr(self, "_health_monitor") and self._health_monitor:
            await self._safe_stop("health_monitor", self._health_monitor.stop())

        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass

        if hasattr(self, "_dlna_buffer_check_task") and self._dlna_buffer_check_task and not self._dlna_buffer_check_task.done():
            self._dlna_buffer_check_task.cancel()
            try:
                await self._dlna_buffer_check_task
            except asyncio.CancelledError:
                pass

        if hasattr(self, "_alarm_scheduler") and self._alarm_scheduler:
            await self._safe_stop("alarm_scheduler", self._alarm_scheduler.stop())

        if hasattr(self, "_scan_scheduler") and self._scan_scheduler:
            await self._safe_stop("scan_scheduler", self._scan_scheduler.stop())

        if hasattr(self, "_autosync") and self._autosync:
            await self._safe_stop("autosync", self._autosync.stop())

        if self._enricher:
            await self._safe_stop("enricher", self._enricher.stop())

        if hasattr(self, "_artist_enrichment") and self._artist_enrichment:
            await self._safe_stop("artist_enrichment", self._artist_enrichment.close())

        if self._watcher:
            await self._safe_stop("watcher", self._watcher.stop())

        if self._sync_engine:
            await self._safe_stop("sync_engine", self._sync_engine.stop())

        if self._ws_manager:
            await self._safe_stop("ws_manager", self._ws_manager.stop())

        if self._mount_manager:
            await self._safe_stop("mount_manager", self._mount_manager.stop())

        if hasattr(self, "_smb_discovery") and self._smb_discovery:
            await self._safe_stop("smb_discovery", self._smb_discovery.stop())

        if hasattr(self, "_spotify_connect") and self._spotify_connect:
            await self._safe_stop("spotify_connect", self._spotify_connect.disable())

        if self._oh_event_listener:
            await self._safe_stop("oh_event_listener", self._oh_event_listener.stop())

        if self._discovery_manager:
            await self._safe_stop("discovery", self._discovery_manager.stop())

        if self._zone_manager:
            await self._safe_stop("zone_manager", self._zone_manager.cleanup())

        if self._http_streamer:
            await self._safe_stop("http_streamer", self._http_streamer.stop())

        # Close all streaming services
        for name, service in list(deps.streaming_services.items()):
            await self._safe_stop(f"streaming:{name}", service.close())

        if self._db:
            await self._safe_stop("database", self._db.close())

        logger.info("tune_server_stopped")


async def run_server(shutdown_event: asyncio.Event | None = None) -> None:
    """Entry point: start the server and run Uvicorn."""
    server = TuneServer()
    try:
        await server.start()
    except Exception:
        logger.exception("tune_server_start_failed")
        await server.stop()
        raise

    # Reuse the SPA-wrapped app the server created during start() — that's
    # the one plugins have already mounted their routes on. Falling back to
    # a fresh app would lose plugin-contributed routers + SPA fallback.
    app = server.serving_app or wrap_for_serving(create_api_app())

    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    uvi_server = uvicorn.Server(config)

    signal_task = None
    if shutdown_event:
        async def _wait_for_signal():
            await shutdown_event.wait()
            uvi_server.should_exit = True
        signal_task = asyncio.create_task(_wait_for_signal())

    try:
        await uvi_server.serve()
    except OSError as exc:
        # Catch port-in-use errors that slip past the pre-flight check
        # (e.g. race condition, or port grabbed between check and bind).
        import errno
        if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048), 10048, 98):
            logger.error(
                "port_in_use",
                port=settings.api_port,
                hint=f"Port {settings.api_port} is already in use. "
                     f"Stop the other process or set TUNE_API_PORT to a different port.",
            )
        else:
            logger.exception("server_bind_error")
        await server.stop()
        raise
    finally:
        if signal_task:
            signal_task.cancel()
        await server.stop()

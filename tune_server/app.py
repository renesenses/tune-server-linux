from __future__ import annotations

import asyncio
import logging

import structlog
import uvicorn

from tune_server.api.deps import deps
from tune_server.api.main import create_api_app, setup_websocket_manager
from tune_server.config import settings
from tune_server.db.engine import Database
from tune_server.db.repository import (
    AlbumRepo,
    ArtistRepo,
    PlaylistRepo,
    PlayQueueRepo,
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
from tune_server.utils.audio_utils import check_ffmpeg
from tune_server.utils.network import get_local_ip
from tune_server.zones.group import GroupManager
from tune_server.zones.manager import ZoneManager
from tune_server.zones.sync import SyncEngine

logger = structlog.get_logger()

COMPONENT_SHUTDOWN_TIMEOUT = 5  # seconds


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
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
        self._http_streamer: HttpAudioStreamer | None = None
        self._mount_manager = None
        self._ws_manager = None
        self._scan_task: asyncio.Task | None = None
        self._server_ip = get_local_ip()

    async def start(self) -> None:
        _configure_logging()
        logger.info("tune_server_starting", version="0.1.0")

        # Startup validation
        from pathlib import Path
        for music_dir in settings.music_dirs:
            if not Path(music_dir).is_dir():
                logger.error("music_dir_not_found", path=music_dir)

        if not check_ffmpeg():
            logger.error("ffmpeg_not_found",
                hint="Install with: sudo apt install ffmpeg")

        # Database
        self._db = Database(settings.db_path)
        await self._db.connect()

        # Repos
        track_repo = TrackRepo(self._db)
        album_repo = AlbumRepo(self._db)
        artist_repo = ArtistRepo(self._db)
        queue_repo = PlayQueueRepo(self._db)
        zone_repo = ZoneRepo(self._db)
        playlist_repo = PlaylistRepo(self._db)
        radio_repo = RadioStationRepo(self._db)

        # Library scanner
        self._scanner = LibraryScanner(self._db, self._event_bus)

        # Zone manager
        self._zone_manager = ZoneManager(self._db, self._event_bus)

        # Group manager
        self._group_manager = GroupManager(self._event_bus)

        # Sync engine
        self._sync_engine = SyncEngine(self._group_manager)

        # HTTP audio streamer for DLNA
        self._http_streamer = HttpAudioStreamer(
            host=settings.stream_host,
            port=settings.stream_port,
        )
        await self._http_streamer.start()

        # Register output factories
        await self._register_output_factories()

        # Discovery — start BEFORE zone init so DLNA devices can be found
        self._discovery_manager = DiscoveryManager(self._event_bus)
        await self._discovery_manager.start()

        # Mount manager for network shares
        if settings.network_shares_enabled or settings.network_media_servers_enabled:
            from tune_server.network.mount_manager import MountManager
            self._mount_manager = MountManager(
                self._db, self._event_bus, self._scanner, settings.smb_mount_dir,
            )
            await self._mount_manager.initialize()
            deps.mount_manager = self._mount_manager

        # Brief wait for initial SSDP scan to find devices
        await asyncio.sleep(2)

        # Initialize zones from DB (now devices should be available)
        await self._zone_manager.initialize()

        # Streaming services
        self._setup_streaming_services()
        await self._restore_streaming_auth()
        self._build_stream_url_resolver()

        # Populate deps for API
        deps.db = self._db
        deps.event_bus = self._event_bus
        deps.scanner = self._scanner
        deps.zone_manager = self._zone_manager
        deps.group_manager = self._group_manager
        deps.discovery_manager = self._discovery_manager
        deps.track_repo = track_repo
        deps.album_repo = album_repo
        deps.artist_repo = artist_repo
        deps.playlist_repo = playlist_repo
        deps.queue_repo = queue_repo
        deps.zone_repo = zone_repo
        deps.radio_repo = radio_repo

        # WebSocket manager
        self._ws_manager = await setup_websocket_manager(self._event_bus)

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

    async def _register_output_factories(self) -> None:
        from tune_server.models import OutputType
        from tune_server.outputs.dlna import DlnaOutput
        from tune_server.outputs.airplay import AirPlayOutput
        from tune_server.outputs.local import LocalOutput

        async def create_dlna_output(device_id: str | None):
            if not device_id or not self._discovery_manager or not self._discovery_manager.ssdp:
                logger.warning("dlna_factory_no_discovery", device_id=device_id)
                return None
            dmr = self._discovery_manager.ssdp.get_dmr_device(device_id)
            if not dmr:
                # Wait for SSDP discovery to find the device (up to 15s)
                logger.info("dlna_factory_waiting_for_device", device_id=device_id)
                for _ in range(15):
                    await asyncio.sleep(1)
                    dmr = self._discovery_manager.ssdp.get_dmr_device(device_id)
                    if dmr:
                        break
            if not dmr:
                logger.warning("dlna_factory_device_not_found", device_id=device_id,
                             available=list(self._discovery_manager.ssdp._dmr_devices.keys()))
                return None
            # Pass device info for DSD detection
            disc_device = self._discovery_manager.ssdp.devices.get(device_id)
            caps = disc_device.capabilities if disc_device else {}
            return DlnaOutput(
                dmr, self._http_streamer, self._server_ip,
                sink_protocols=caps.get("sink_protocols", []),
                device_name=caps.get("device_name", ""),
                device_model=caps.get("model", ""),
            )

        async def create_airplay_output(device_id: str | None):
            if not device_id or not self._discovery_manager or not self._discovery_manager.mdns:
                return None
            config = self._discovery_manager.mdns.get_atv_config(device_id)
            if not config:
                return None
            try:
                import pyatv

                # Load saved credentials from DB
                if self._db:
                    row = await self._db.execute(
                        "SELECT credentials FROM device_credentials WHERE device_id = ?",
                        (device_id,),
                    )
                    cred_row = await row.fetchone()
                    if cred_row and cred_row[0]:
                        creds = cred_row[0]
                        for protocol in [pyatv.Protocol.AirPlay, pyatv.Protocol.RAOP, pyatv.Protocol.Companion]:
                            if config.get_service(protocol) is not None:
                                config.set_credentials(protocol, creds)
                        logger.info("airplay_credentials_loaded", device_id=device_id)

                atv = await pyatv.connect(config, asyncio.get_running_loop())
                device = self._discovery_manager.get_device(device_id)
                name = device.name if device else "AirPlay"
                return AirPlayOutput(atv, device_name=name)
            except Exception:
                logger.exception("airplay_connect_error", device_id=device_id)
                return None

        async def create_local_output(device_id: str | None):
            return LocalOutput(device_name=device_id)

        self._zone_manager.register_output_factory(OutputType.DLNA, create_dlna_output)
        self._zone_manager.register_output_factory(OutputType.AIRPLAY, create_airplay_output)
        self._zone_manager.register_output_factory(OutputType.LOCAL, create_local_output)

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
            deps.streaming_services["deezer"] = DeezerService()
            logger.info("deezer_service_enabled")

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

    async def stop(self) -> None:
        logger.info("tune_server_stopping")

        await self._event_bus.emit(Event(
            type=EventType.SYSTEM_STOPPING,
            source="app",
        ))

        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass

        if self._enricher:
            await self._safe_stop("enricher", self._enricher.stop())

        if self._watcher:
            await self._safe_stop("watcher", self._watcher.stop())

        if self._sync_engine:
            await self._safe_stop("sync_engine", self._sync_engine.stop())

        if self._ws_manager:
            await self._safe_stop("ws_manager", self._ws_manager.stop())

        if self._mount_manager:
            await self._safe_stop("mount_manager", self._mount_manager.stop())

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

    app = create_api_app()

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
    finally:
        if signal_task:
            signal_task.cancel()
        await server.stop()

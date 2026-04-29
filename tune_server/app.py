from __future__ import annotations

import asyncio
import logging

import structlog
import uvicorn

from tune_server.api.deps import deps
from tune_server.api.main import create_api_app, setup_websocket_manager
from tune_server.config import settings
from tune_server.db.factory import create_database
from tune_server.db.repository import (
    AlbumRepo,
    ArtistRepo,
    PlaylistRepo,
    PlayQueueRepo,
    RadioFavoriteRepo,
    RadioStationRepo,
    TrackCreditRepo,
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
        else:
            # PyInstaller bundle (Windows .exe / Linux binary): log next
            # to the running binary.
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
        self._http_streamer: HttpAudioStreamer | None = None
        self._mount_manager = None
        self._ws_manager = None
        self._scan_task: asyncio.Task | None = None
        self._server_ip = get_local_ip()

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

        # Database — use SQLAlchemy Core engine (database-independent)
        self._db = create_database(settings, use_sa=True)
        await self._db.connect()

        # Repos — SA Core (database-independent)
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

        # Track credit repo
        credit_repo = TrackCreditRepo(self._db)

        # Library scanner
        self._scanner = LibraryScanner(self._db, self._event_bus, credit_repo=credit_repo)

        # Zone manager
        self._zone_manager = ZoneManager(self._db, self._event_bus)

        # Group manager
        self._group_manager = GroupManager(self._event_bus)
        self._zone_manager.set_group_manager(self._group_manager)

        # Sync engine
        self._sync_engine = SyncEngine(self._group_manager)

        # Snapcast manager — Linux/macOS only, no-op when snapserver
        # binary is missing. Owned here so the OutputType.SNAPCAST
        # factory below can reference it. v0.8.0 milestone.
        from tune_server.zones.snapcast_manager import SnapcastManager
        snapcast_runtime = (
            Path(settings.snapcast_runtime_dir)
            if settings.snapcast_runtime_dir
            else (Path(settings.data_dir) if hasattr(settings, "data_dir") else Path.cwd()) / "snapcast"
        )
        snapcast_runtime.mkdir(parents=True, exist_ok=True)
        snapcast_binary = (
            Path(settings.snapcast_binary)
            if settings.snapcast_binary else None
        )
        self._snapcast_manager = SnapcastManager(
            runtime_dir=snapcast_runtime,
            binary=snapcast_binary,
        ) if settings.snapcast_enabled else None

        # HTTP audio streamer for DLNA
        self._http_streamer = HttpAudioStreamer(
            host=settings.stream_host,
            port=settings.stream_port,
        )


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

        await self._http_streamer.start()

        # Now that the streamer is up, tell the Deezer service where to
        # build proxy URLs (used by get_stream_url).
        if "deezer" in deps.streaming_services:
            base = f"http://{self._server_ip}:{settings.stream_port}"
            deps.streaming_services["deezer"].set_proxy_base_url(base)

        if self._upnp_server:
            await self._upnp_server.start()



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

        # WebSocket manager — start BEFORE zones so events are never lost
        self._ws_manager = await setup_websocket_manager(self._event_bus)

        # Brief wait for initial SSDP scan to find devices
        await asyncio.sleep(2)

        # Initialize zones from DB (now devices should be available)
        await self._zone_manager.initialize()

        # Retry unavailable zones after discovery has had more time
        async def _retry_zones():
            await asyncio.sleep(60)
            await self._zone_manager.retry_pending_zones()
        asyncio.create_task(_retry_zones())

        # Streaming services already created above (before http_streamer.start
        # so the Deezer proxy could register routes). Restore auth now that
        # the DB is available.
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
        deps.credit_repo = credit_repo
        from tune_server.db.repository import PlaybackHistoryRepo
        history_repo = PlaybackHistoryRepo(self._db)
        deps.history_repo = history_repo
        self._setup_playback_history(history_repo)
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
        deps.snapcast_manager = self._snapcast_manager
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

        # Start Snapcast manager (no-op on unsupported platforms / missing binary).
        if self._snapcast_manager is not None:
            await self._snapcast_manager.start()

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
            except ImportError:
                raise RuntimeError("AirPlay: pyatv library is not installed")
            except RuntimeError:
                raise
            except Exception as exc:
                logger.exception("airplay_connect_error", device_id=device_id)
                raise RuntimeError(f"AirPlay: connection failed — {exc}") from exc

        async def create_local_output(device_id: str | None):
            return LocalOutput(device_name=device_id)

        self._zone_manager.register_output_factory(OutputType.DLNA, create_dlna_output)
        self._zone_manager.register_output_factory(OutputType.AIRPLAY, create_airplay_output)
        self._zone_manager.register_output_factory(OutputType.LOCAL, create_local_output)

        # Snapcast — gated to Linux/macOS, manager owns the snapserver
        # subprocess + JSON-RPC. The factory only registers when the
        # manager actually started (binary present); on Windows or
        # when snapserver is missing we leave OutputType.SNAPCAST
        # unhandled and `manager._create_output` returns None with a
        # clear log so the API can refuse gracefully. v0.8.0 milestone.
        if (
            settings.snapcast_enabled
            and self._snapcast_manager is not None
            and self._snapcast_manager.is_supported
            and self._snapcast_manager.binary_path is not None
        ):
            async def create_snapcast_output(device_id: str | None):
                # device_id here is the zone_id (str). v0.8.0 task #45
                # will plumb this through register_stream + return a
                # SnapcastOutput bound to the per-zone FIFO.
                from tune_server.outputs.snapcast import SnapcastOutput
                logger.info("snapcast_factory_called_skeleton", device_id=device_id)
                stream_name, fifo = await self._snapcast_manager.register_stream(
                    int(device_id) if device_id and device_id.isdigit() else 0,
                    sample_rate=44100, bit_depth=16,
                )
                return SnapcastOutput(self._snapcast_manager, stream_name, fifo)

            self._zone_manager.register_output_factory(
                OutputType.SNAPCAST, create_snapcast_output,
            )

    def _setup_playback_history(self, history_repo) -> None:
        """Record each played track in playback_history via EventBus."""
        async def _on_track_changed(event: Event):
            data = event.data or {}
            track_id = data.get("track_id")
            zone_id = data.get("zone_id")
            # Fetch full track info from the zone's current track
            zone = self._zone_manager.get_zone(zone_id) if zone_id else None
            track = zone.player._queue.current if zone else None
            if track:
                try:
                    # Resolve cover: track cover_path, fallback to album cover
                    cover = track.cover_path
                    if not cover and track.album_id and deps.album_repo:
                        album = await deps.album_repo.get(track.album_id)
                        if album:
                            cover = album.cover_path
                    await history_repo.record(
                        track_id=track.id,
                        zone_id=zone_id,
                        track_title=track.title,
                        artist_name=track.artist_name,
                        album_title=track.album_title,
                        cover_path=cover,
                        duration_ms=track.duration_ms,
                        listened_ms=zone.player.position_ms if zone else None,
                        source=track.source.value if track.source else None,
                    )
                except Exception:
                    logger.debug("playback_history_record_error", exc_info=True)

        self._event_bus.on(EventType.PLAYBACK_TRACK_CHANGED, _on_track_changed)

        # Last.fm scrobbling
        if settings.lastfm_scrobble_enabled and settings.lastfm_api_key and settings.lastfm_api_secret:
            from tune_server.metadata.lastfm_scrobbler import LastfmScrobbler
            scrobbler = LastfmScrobbler(
                api_key=settings.lastfm_api_key,
                api_secret=settings.lastfm_api_secret,
                session_key=settings.lastfm_session_key or None,
            )

            async def _on_scrobble(event: Event):
                data = event.data or {}
                zone_id = data.get("zone_id")
                zone = self._zone_manager.get_zone(zone_id) if zone_id else None
                track = zone.player._queue.current if zone else None
                if track and track.artist_name and scrobbler.is_authenticated:
                    await scrobbler.scrobble(
                        artist=track.artist_name,
                        track=track.title,
                        album=track.album_title,
                        duration=track.duration_ms,
                    )

            self._event_bus.on(EventType.PLAYBACK_TRACK_CHANGED, _on_scrobble)
            logger.info("lastfm_scrobbling_enabled")

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

        if self._snapcast_manager is not None:
            await self._safe_stop("snapcast", self._snapcast_manager.stop())

        if self._ws_manager:
            await self._safe_stop("ws_manager", self._ws_manager.stop())

        if self._mount_manager:
            await self._safe_stop("mount_manager", self._mount_manager.stop())

        if hasattr(self, "_spotify_connect") and self._spotify_connect:
            await self._safe_stop("spotify_connect", self._spotify_connect.disable())

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

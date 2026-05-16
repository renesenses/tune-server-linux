from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tune_server.db.engine import Database
    from tune_server.db.repository import AlbumRatingRepo, AlbumRepo, ArtistRepo, PartyVoteRepo, PlaybackHistoryRepo, PlaylistRepo, PlayQueueRepo, RadioFavoriteRepo, RadioStationRepo, TrackCreditRepo, TrackRepo, ZoneRepo
    from tune_server.discovery.manager import DiscoveryManager
    from tune_server.event_bus import EventBus
    from tune_server.library.enrichment import MetadataEnricher
    from tune_server.library.scan_scheduler import ScanScheduler
    from tune_server.library.scanner import LibraryScanner
    from tune_server.library.watcher import FileSystemWatcher
    from tune_server.metadata.artist_enrichment import ArtistEnrichmentClient
    from tune_server.network.mount_manager import MountManager
    from tune_server.plugins.loader import PluginLoader
    from tune_server.plugins.store import PluginStoreManager
    from tune_server.spotify_connect import SpotifyConnectManager
    from tune_server.streaming.base import StreamingService
    from tune_server.updater import UpdateChecker
    from tune_server.zones.group import GroupManager
    from tune_server.zones.manager import ZoneManager


class AppDeps:
    """Container for application dependencies, injected into FastAPI routes."""

    def __init__(self) -> None:
        self.db: Database | None = None
        self.event_bus: EventBus | None = None
        self.scanner: LibraryScanner | None = None
        self.scan_scheduler: ScanScheduler | None = None
        self.zone_manager: ZoneManager | None = None
        self.group_manager: GroupManager | None = None
        self.discovery_manager: DiscoveryManager | None = None
        self.mount_manager: MountManager | None = None
        self.streaming_services: dict[str, StreamingService] = {}
        self.watcher: FileSystemWatcher | None = None
        self.enricher: MetadataEnricher | None = None
        self.stream_url_resolver: object | None = None  # StreamUrlResolver callable
        self.update_checker: UpdateChecker | None = None
        self.artist_enrichment: ArtistEnrichmentClient | None = None
        self.spotify_connect: SpotifyConnectManager | None = None
        self.plugin_loader: PluginLoader | None = None
        self.store_manager: PluginStoreManager | None = None

        # Repos (set after DB init)
        self.track_repo: TrackRepo | None = None
        self.album_repo: AlbumRepo | None = None
        self.artist_repo: ArtistRepo | None = None
        self.playlist_repo: PlaylistRepo | None = None
        self.queue_repo: PlayQueueRepo | None = None
        self.zone_repo: ZoneRepo | None = None
        self.radio_repo: RadioStationRepo | None = None
        self.radio_fav_repo: RadioFavoriteRepo | None = None
        self.credit_repo: TrackCreditRepo | None = None
        self.history_repo: PlaybackHistoryRepo | None = None
        self.party_vote_repo: PartyVoteRepo | None = None
        self.album_rating_repo: AlbumRatingRepo | None = None

    @property
    def tidal(self):
        return self.streaming_services.get("tidal")

    @tidal.setter
    def tidal(self, value):
        if value is None:
            self.streaming_services.pop("tidal", None)
        else:
            self.streaming_services["tidal"] = value

    @property
    def qobuz(self):
        return self.streaming_services.get("qobuz")

    @qobuz.setter
    def qobuz(self, value):
        if value is None:
            self.streaming_services.pop("qobuz", None)
        else:
            self.streaming_services["qobuz"] = value

    @property
    def streaming_manager(self) -> "_StreamingManagerFacade":
        return _StreamingManagerFacade(self.streaming_services)


class _StreamingManagerFacade:
    """Thin wrapper exposing `.service(name)` and `.status` over the streaming_services dict."""

    def __init__(self, services: dict) -> None:
        self._services = services

    def service(self, name: str):
        return self._services.get(name)

    @property
    def status(self) -> dict[str, bool]:
        return {name: svc.is_authenticated for name, svc in self._services.items()}


# Global instance — populated during app startup
deps = AppDeps()

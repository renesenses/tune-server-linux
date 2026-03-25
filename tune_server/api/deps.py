from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tune_server.db.engine import Database
    from tune_server.db.repository import AlbumRepo, ArtistRepo, PlaylistRepo, PlayQueueRepo, RadioStationRepo, TrackRepo, ZoneRepo
    from tune_server.discovery.manager import DiscoveryManager
    from tune_server.event_bus import EventBus
    from tune_server.library.scanner import LibraryScanner
    from tune_server.library.watcher import FileSystemWatcher
    from tune_server.network.mount_manager import MountManager
    from tune_server.streaming.base import StreamingService
    from tune_server.zones.group import GroupManager
    from tune_server.zones.manager import ZoneManager


class AppDeps:
    """Container for application dependencies, injected into FastAPI routes."""

    def __init__(self) -> None:
        self.db: Database | None = None
        self.event_bus: EventBus | None = None
        self.scanner: LibraryScanner | None = None
        self.zone_manager: ZoneManager | None = None
        self.group_manager: GroupManager | None = None
        self.discovery_manager: DiscoveryManager | None = None
        self.mount_manager: MountManager | None = None
        self.streaming_services: dict[str, StreamingService] = {}
        self.watcher: FileSystemWatcher | None = None
        self.stream_url_resolver: object | None = None  # StreamUrlResolver callable

        # Repos (set after DB init)
        self.track_repo: TrackRepo | None = None
        self.album_repo: AlbumRepo | None = None
        self.artist_repo: ArtistRepo | None = None
        self.playlist_repo: PlaylistRepo | None = None
        self.queue_repo: PlayQueueRepo | None = None
        self.zone_repo: ZoneRepo | None = None
        self.radio_repo: RadioStationRepo | None = None

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


# Global instance — populated during app startup
deps = AppDeps()

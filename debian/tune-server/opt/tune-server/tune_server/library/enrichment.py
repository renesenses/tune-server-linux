from __future__ import annotations

import asyncio

import structlog

from tune_server.db.engine import Database
from tune_server.db.repository import AlbumRepo, ArtistRepo

logger = structlog.get_logger()


class MetadataEnricher:
    """Background enrichment from MusicBrainz and Discogs."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._artist_repo = ArtistRepo(db)
        self._album_repo = AlbumRepo(db)
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._enrich_loop())
        logger.info("metadata_enricher_started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _enrich_loop(self) -> None:
        try:
            import musicbrainzngs
            musicbrainzngs.set_useragent("TuneServer", "0.1.0", "")
        except ImportError:
            logger.warning("musicbrainzngs_not_installed")
            return

        while self._running:
            try:
                # Find artists without MusicBrainz IDs
                artists = await self._artist_repo.list(limit=10)
                for artist in artists:
                    if artist.musicbrainz_id or not self._running:
                        continue

                    try:
                        result = await asyncio.to_thread(
                            musicbrainzngs.search_artists,
                            artist=artist.name,
                            limit=1,
                        )
                        mb_artists = result.get("artist-list", [])
                        if mb_artists:
                            mb = mb_artists[0]
                            artist.musicbrainz_id = mb.get("id")
                            artist.sort_name = mb.get("sort-name", artist.sort_name)
                            if not artist.bio:
                                artist.bio = mb.get("disambiguation")
                            await self._artist_repo.update(artist)
                            logger.debug("artist_enriched", name=artist.name, mb_id=artist.musicbrainz_id)
                    except Exception:
                        logger.debug("musicbrainz_lookup_failed", artist=artist.name)

                    # Rate limit: MusicBrainz allows 1 request per second
                    await asyncio.sleep(1.5)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("enrichment_loop_error")

            # Wait before next enrichment pass
            await asyncio.sleep(300)  # 5 minutes

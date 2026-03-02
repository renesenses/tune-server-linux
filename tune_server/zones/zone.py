from __future__ import annotations

import json

import structlog

from tune_server.db.repository import PlayQueueRepo, ZoneRepo
from tune_server.event_bus import EventBus
from tune_server.models import AudioFormat, OutputType, PlaybackState, Source, Track, Zone
from tune_server.outputs.base import OutputTarget
from tune_server.playback.player import Player

logger = structlog.get_logger()


class ZoneInstance:
    """A zone combines a player, queue, and output target."""

    def __init__(
        self,
        zone_id: int,
        name: str,
        output_type: OutputType,
        output: OutputTarget,
        event_bus: EventBus,
        queue_repo: PlayQueueRepo | None = None,
        zone_repo: ZoneRepo | None = None,
    ) -> None:
        self._zone_id = zone_id
        self._name = name
        self._output_type = output_type
        self._output = output
        self._queue_repo = queue_repo
        self._zone_repo = zone_repo
        self._player = Player(zone_id, event_bus)
        self._player.set_output(output)
        if queue_repo:
            self._player.set_queue_persist_callback(self._save_queue)
        if zone_repo:
            self._player.set_volume_change_callback(self.save_volume)
        self._group_id: str | None = None
        self._sync_delay_ms: int = 0

    @property
    def zone_id(self) -> int:
        return self._zone_id

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def output_type(self) -> OutputType:
        return self._output_type

    @property
    def output(self) -> OutputTarget:
        return self._output

    @property
    def player(self) -> Player:
        return self._player

    @property
    def group_id(self) -> str | None:
        return self._group_id

    @group_id.setter
    def group_id(self, value: str | None) -> None:
        self._group_id = value

    @property
    def sync_delay_ms(self) -> int:
        return self._sync_delay_ms

    @sync_delay_ms.setter
    def sync_delay_ms(self, value: int) -> None:
        self._sync_delay_ms = value

    @property
    def state(self) -> PlaybackState:
        return self._player.state

    @property
    def current_track(self) -> Track | None:
        return self._player.current_track

    @property
    def position_ms(self) -> int:
        return self._player.position_ms

    def to_model(self) -> Zone:
        return Zone(
            id=self._zone_id,
            name=self._name,
            output_type=self._output_type,
            volume=self._player.volume,
            group_id=self._group_id,
            sync_delay_ms=self._sync_delay_ms,
            state=self._player.state,
            current_track=self._player.current_track,
            position_ms=self._player.position_ms,
            queue_length=self._player.queue.length,
        )

    async def _save_queue(self, tracks: list[Track], position: int) -> None:
        if not self._queue_repo:
            return

        # Serialize full queue as JSON (handles streaming tracks with no DB id)
        queue_data = {
            "position": position,
            "tracks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "album_id": t.album_id,
                    "album_title": t.album_title,
                    "artist_id": t.artist_id,
                    "artist_name": t.artist_name,
                    "disc_number": t.disc_number,
                    "track_number": t.track_number,
                    "duration_ms": t.duration_ms,
                    "file_path": t.file_path if t.source in (Source.LOCAL, Source.RADIO) else None,
                    "format": t.format.value if t.format else None,
                    "sample_rate": t.sample_rate,
                    "bit_depth": t.bit_depth,
                    "channels": t.channels,
                    "source": t.source.value if t.source else None,
                    "source_id": t.source_id,
                }
                for t in tracks
            ],
        }
        if self._zone_repo:
            await self._zone_repo.update(self._zone_id, queue_json=json.dumps(queue_data))

        # Backward compat: also write local track IDs to play_queue table
        track_ids = [t.id for t in tracks if t.id is not None]
        if track_ids:
            await self._queue_repo.set_queue(self._zone_id, track_ids)
            # Map position to local-only index
            local_pos = sum(1 for t in tracks[:position] if t.id is not None)
            if 0 <= local_pos < len(track_ids):
                await self._queue_repo.set_current(self._zone_id, local_pos)

    async def restore_queue(self) -> None:
        """Restore queue from DB on startup. Tries JSON first, falls back to play_queue."""
        if not self._queue_repo:
            return

        # Try JSON-based queue first (supports streaming tracks)
        if self._zone_repo:
            row = await self._zone_repo.get(self._zone_id)
            if row and row.get("queue_json"):
                try:
                    queue_data = json.loads(row["queue_json"])
                    tracks = []
                    for t in queue_data["tracks"]:
                        track = Track(
                            id=t.get("id"),
                            title=t["title"],
                            album_id=t.get("album_id"),
                            album_title=t.get("album_title"),
                            artist_id=t.get("artist_id"),
                            artist_name=t.get("artist_name"),
                            disc_number=t.get("disc_number", 1),
                            track_number=t.get("track_number", 0),
                            duration_ms=t.get("duration_ms", 0),
                            file_path=t.get("file_path"),
                            format=AudioFormat(t["format"]) if t.get("format") else None,
                            sample_rate=t.get("sample_rate"),
                            bit_depth=t.get("bit_depth"),
                            channels=t.get("channels", 2),
                            source=Source(t["source"]) if t.get("source") else Source.LOCAL,
                            source_id=t.get("source_id"),
                        )
                        tracks.append(track)
                    if tracks:
                        position = queue_data.get("position", 0)
                        self._player.queue.set_tracks(tracks, position)
                        logger.info("queue_restored_json", zone_id=self._zone_id, tracks=len(tracks))
                        return
                except (json.JSONDecodeError, KeyError, ValueError):
                    logger.warning("queue_json_parse_error", zone_id=self._zone_id)

        # Fallback: restore from play_queue table (local tracks only)
        rows = await self._queue_repo.get_queue(self._zone_id)
        if not rows:
            return
        tracks = []
        current_pos = 0
        for i, row in enumerate(rows):
            track = Track(
                id=row["track_id"],
                title=row["title"],
                file_path=row["file_path"],
                duration_ms=row["duration_ms"],
                format=AudioFormat(row["format"]) if row.get("format") else None,
                sample_rate=row["sample_rate"],
                bit_depth=row["bit_depth"],
                channels=row["channels"],
                album_title=row.get("album_title"),
                artist_name=row.get("artist_name"),
            )
            tracks.append(track)
            if row.get("is_current"):
                current_pos = i
        if tracks:
            self._player.queue.set_tracks(tracks, current_pos)
            logger.info("queue_restored", zone_id=self._zone_id, tracks=len(tracks))

    async def save_volume(self, volume: float) -> None:
        if self._zone_repo:
            await self._zone_repo.update(self._zone_id, volume=volume)

    async def cleanup(self) -> None:
        await self._player.cleanup()

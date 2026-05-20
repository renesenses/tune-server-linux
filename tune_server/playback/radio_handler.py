from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from tune_server.models import Source

if TYPE_CHECKING:
    from tune_server.playback.player import Player

logger = structlog.get_logger()


class RadioMetadataHandler:
    """Handles ICY metadata polling and RadioFrance API fallback for radio streams.

    Owns the ``_icy_poller_task`` and ``_radio_poller`` state that was
    previously on Player directly.
    """

    def __init__(self, player: Player) -> None:
        self._player = player
        self._icy_poller_task: asyncio.Task | None = None
        self._radio_poller = None

    # -- public helpers -----------------------------------------------------

    @property
    def radio_poller(self):
        return self._radio_poller

    def stop(self) -> None:
        """Cancel all radio metadata pollers."""
        if self._icy_poller_task:
            self._icy_poller_task.cancel()
            self._icy_poller_task = None
        if self._radio_poller:
            self._radio_poller.stop()
            self._radio_poller = None

    # -- extracted methods --------------------------------------------------

    def make_icy_callback(self, track):
        """Create a callback that updates the current track with ICY metadata."""
        from tune_server.playback.player import cover_url_for_client
        from tune_server.event_bus import Event, EventType

        p = self._player
        zone_id = p._zone_id
        event_bus = p._event_bus
        station_name = track.title  # original station name
        station_cover = track.cover_path  # original station logo (fallback)

        def on_icy_metadata(meta: dict[str, str]) -> None:
            on_icy_metadata._received = True
            current = p._queue.current
            if not current or current.source != Source.RADIO:
                return

            icy_title = meta.get("title", "")
            icy_artist = meta.get("artist", "")

            if not icy_title and not icy_artist:
                return

            # Update the track metadata in-place
            if icy_artist:
                current.artist_name = icy_artist
                current.title = icy_title or station_name
            else:
                # No separator found -- put raw title in album_title
                current.title = icy_title
                current.artist_name = station_name

            current.album_title = station_name  # keep station name accessible

            # Update cover: use RadioFrance cover if available, else station logo
            cover_url = meta.get("cover_url")
            current.cover_path = cover_url or station_cover

            logger.info("icy_metadata_update", station=station_name, title=icy_title, artist=icy_artist)

            # Emit event so WebSocket clients can update
            event_bus.emit_nowait(Event(
                type=EventType.PLAYBACK_METADATA,
                data={
                    "zone_id": zone_id,
                    "title": current.title,
                    "artist_name": current.artist_name,
                    "album_title": current.album_title,
                    "cover_path": cover_url_for_client(current.cover_path),
                    "source": "radio",
                },
                source="player",
            ))

        return on_icy_metadata

    async def poll_icy_metadata(self, stream_url: str, callback) -> None:
        """Poll ICY metadata from a radio stream URL independently of the audio pipeline."""
        import aiohttp
        from tune_server.models import PlaybackState

        p = self._player
        try:
            headers = {"Icy-MetaData": "1", "User-Agent": "TuneServer/1.0"}
            timeout = aiohttp.ClientTimeout(total=0)  # no timeout for radio
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(stream_url, headers=headers) as resp:
                    metaint = int(resp.headers.get("icy-metaint", "0"))
                    if metaint == 0:
                        return  # no ICY support

                    audio_bytes = 0
                    async for chunk in resp.content.iter_any():
                        if p._state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                            break

                        # Skip audio data, just track byte count for metadata boundaries
                        audio_bytes += len(chunk)
                        while audio_bytes >= metaint:
                            # We've passed a metadata boundary -- read next metadata block
                            audio_bytes -= metaint
                            # Read the metadata length byte
                            meta_len_data = await resp.content.readexactly(1)
                            meta_len = meta_len_data[0] * 16
                            if meta_len > 0:
                                meta_data = await resp.content.readexactly(meta_len)
                                meta_str = meta_data.decode("utf-8", errors="ignore").rstrip("\x00")
                                # Parse StreamTitle='Artist - Title';
                                for part in meta_str.split(";"):
                                    part = part.strip()
                                    if part.lower().startswith("streamtitle="):
                                        title = part.split("=", 1)[1].strip("'\"")
                                        if title:
                                            artist, track_title = "", title
                                            if " - " in title:
                                                artist, track_title = title.split(" - ", 1)
                                            callback({"title": track_title.strip(), "artist": artist.strip()})
        except asyncio.CancelledError:
            logger.debug("icy_poller_cancelled", zone_id=p._zone_id)
        except Exception:
            logger.debug("icy_poller_stopped", zone_id=p._zone_id)

    def start_radio_poller(self, track, icy_cb) -> None:
        """Start the RadioMetadataPoller for a DLNA radio stream."""
        from tune_server.streaming.radio_metadata import RadioMetadataPoller
        self._radio_poller = RadioMetadataPoller(
            self._player._event_bus, self._player._zone_id, track_callback=icy_cb,
        )
        self._radio_poller.start(track.file_path)

    def schedule_radio_fallback(self, track, icy_cb) -> None:
        """Schedule a fallback radio poller if ICY metadata doesn't arrive within 5s."""
        from tune_server.models import PlaybackState

        p = self._player

        async def _radio_metadata_fallback():
            await asyncio.sleep(5)
            if p._state != PlaybackState.PLAYING:
                return
            if not getattr(icy_cb, '_received', False):
                from tune_server.streaming.radio_metadata import RadioMetadataPoller
                self._radio_poller = RadioMetadataPoller(
                    p._event_bus, p._zone_id, track_callback=icy_cb,
                )
                self._radio_poller.start(track.file_path)
                logger.info("radio_metadata_fallback_started", zone_id=p._zone_id)

        asyncio.create_task(_radio_metadata_fallback())

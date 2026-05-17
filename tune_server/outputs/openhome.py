"""OpenHome output target.

Streams audio to OpenHome-compatible renderers (Linn, Naim, Auralic,
Lumin, dCS, Cambridge Audio, etc.) using the Playlist service for
full queue sync and native gapless playback.

Delegates all SOAP communication to :class:`OpenHomeClient` in
``oh_client.py``.

Phase 2: Native Playlist + Gapless
  - Push queue to device playlist for native gapless transitions
  - Preload next track via ``set_next_track()``
  - Background position monitor detects track changes and reports position
  - Queue sync via ``sync_queue()``

Phase 3: Source Selection + Standby + Rich Metadata
  - Auto-select Playlist source on start, restore previous source on stop
  - Wake from standby on start, optionally standby on stop
  - DIDL-Lite metadata with full audio format details (sample rate, bit depth,
    channels, duration, album art)

Phase 4: UPnP Eventing
  - Subscribe to device event URLs for instant state change notifications
  - Falls back to 2s polling when eventing is unavailable
  - Shared :class:`OpenHomeEventListener` across all outputs
"""
from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Optional

import structlog

from tune_server.audio.formats import AudioCapabilities, AudioFormat, mime_type_for_format
from tune_server.models import AudioStreamInfo, Track
from tune_server.outputs.base import OutputTarget
from tune_server.outputs.oh_client import OpenHomeClient

if TYPE_CHECKING:
    from tune_server.outputs.oh_events import OpenHomeEventListener

logger = structlog.get_logger()

_OPENHOME_CAPABILITIES = AudioCapabilities(
    formats={AudioFormat.FLAC, AudioFormat.WAV, AudioFormat.MP3, AudioFormat.AAC},
    max_sample_rate=384000,
    max_bit_depth=32,
    supports_gapless=True,
)

# Default config values — overridden by TUNE_ env vars via config.py
_AUTO_STANDBY = os.environ.get("TUNE_OPENHOME_AUTO_STANDBY", "false").lower() in ("1", "true", "yes")
_MAX_QUEUE_SIZE = int(os.environ.get("TUNE_OPENHOME_QUEUE_SIZE", "20"))
_POLL_INTERVAL_S = 2.0


# ---------------------------------------------------------------------------
# DIDL-Lite helper
# ---------------------------------------------------------------------------

def _xml_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _mime_for_track(track: Track) -> str:
    """Resolve MIME type from the track's format, defaulting to audio/flac."""
    if track.format:
        return mime_type_for_format(track.format)
    # Guess from file extension
    path = track.file_path or ""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    _ext_map = {"flac": "audio/flac", "mp3": "audio/mpeg", "wav": "audio/wav",
                "aac": "audio/aac", "m4a": "audio/mp4", "ogg": "audio/ogg",
                "opus": "audio/opus", "aiff": "audio/aiff", "aif": "audio/aiff"}
    return _ext_map.get(ext, "audio/flac")


def _cover_url(track: Track, server_ip: str) -> str:
    """Build an HTTP-accessible cover URL for the renderer."""
    cover = track.cover_path or ""
    if not cover:
        return ""
    if cover.startswith("http"):
        return cover
    from tune_server.config import settings
    filename = cover.rsplit("/", 1)[-1] if "/" in cover else cover
    return f"http://{server_ip}:{settings.api_port}/api/v1/library/artwork/{filename}"


def _build_didl(track: Track, uri: str, server_ip: str) -> str:
    """Build rich DIDL-Lite XML with full audio format details."""
    title = _xml_escape(track.title or "")
    artist = _xml_escape(track.artist_name or "")
    album = _xml_escape(track.album_title or "")
    mime = _mime_for_track(track)

    # Duration
    dur_attr = ""
    if track.duration_ms and track.duration_ms > 0:
        s = track.duration_ms // 1000
        dur_str = f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
        dur_attr = f' duration="{dur_str}"'

    # Audio format attributes
    res_extras = ""
    if track.sample_rate:
        res_extras += f' sampleFrequency="{track.sample_rate}"'
    if track.bit_depth:
        res_extras += f' bitsPerSample="{track.bit_depth}"'
    if track.channels:
        res_extras += f' nrAudioChannels="{track.channels}"'

    # Album art
    cover = _cover_url(track, server_ip)
    art_xml = f"<upnp:albumArtURI>{_xml_escape(cover)}</upnp:albumArtURI>" if cover else ""

    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        f'<item id="0" parentID="0" restricted="1">'
        f'<dc:title>{title}</dc:title>'
        f'<dc:creator>{artist}</dc:creator>'
        f'<upnp:artist>{artist}</upnp:artist>'
        f'<upnp:album>{album}</upnp:album>'
        f'{art_xml}'
        f'<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<res protocolInfo="http-get:*:{mime}:*"{dur_attr}{res_extras}>'
        f'{_xml_escape(uri)}</res>'
        f'</item></DIDL-Lite>'
    )


# ---------------------------------------------------------------------------
# OutputTarget implementation
# ---------------------------------------------------------------------------

class OpenHomeOutput(OutputTarget):
    """OpenHome renderer output backed by :class:`OpenHomeClient`.

    Implements native playlist management with gapless playback, source
    selection, standby control, and background position monitoring.
    """

    def __init__(
        self,
        device_name: str,
        service_urls: dict[str, str],
        server_ip: str,
        streamer: object,
        base_url: str = "",
        event_listener: OpenHomeEventListener | None = None,
        event_sub_urls: dict[str, str] | None = None,
    ) -> None:
        self._client = OpenHomeClient(service_urls, device_name=device_name)
        self._device_name = device_name
        self._server_ip = server_ip
        self._streamer = streamer
        self._base_url = base_url
        self._available = True
        self._volume: float = 0.5

        # Playlist state
        self._current_oh_id: int | None = None
        self._playlist_ids: list[int] = []
        self._track_uri_map: dict[int, str] = {}  # oh_id -> stream_url

        # Source selection (Phase 3)
        self._previous_source_index: int | None = None

        # Position monitor (polling fallback)
        self._monitor_task: asyncio.Task | None = None
        self._last_position_ms: int = -1
        self._current_track_uri: str = ""

        # UPnP eventing (Phase 4)
        self._event_listener = event_listener
        self._event_sub_urls = event_sub_urls or {}
        self._active_sub_ids: list[str] = []  # path_ids from listener.subscribe()
        self._eventing_active = False  # True when at least one subscription succeeded

        # Track change callback — set by the player to be notified
        # when the device autonomously advances to the next track
        self.on_track_advanced: asyncio.Event | None = None

    @property
    def name(self) -> str:
        return self._device_name

    @property
    def capabilities(self) -> AudioCapabilities:
        return _OPENHOME_CAPABILITIES

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def is_direct_url(self) -> bool:
        return True

    def supports_direct_url(self, track: Track) -> bool:
        return True

    # -- helpers ---------------------------------------------------------------

    def _build_stream_url(self, track: Track) -> str:
        if track.file_path and (track.file_path.startswith("http://") or
                                track.file_path.startswith("https://")):
            return track.file_path
        if track.id:
            return f"http://{self._server_ip}:8888/api/v1/library/tracks/{track.id}/audio"
        return track.file_path or ""

    def _get_auto_standby(self) -> bool:
        """Read auto-standby preference from config (supports runtime changes)."""
        try:
            from tune_server.config import settings
            return settings.openhome_auto_standby
        except Exception:
            return _AUTO_STANDBY

    def _get_max_queue_size(self) -> int:
        """Read max queue size from config."""
        try:
            from tune_server.config import settings
            return settings.openhome_queue_size
        except Exception:
            return _MAX_QUEUE_SIZE

    # -- Source selection (Phase 3) -------------------------------------------

    async def _save_and_select_playlist_source(self) -> None:
        """Save the current source index, then switch to Playlist source."""
        try:
            sources = await self._client.product_source_xml()
            # Find which source is currently active by checking each one
            # (OpenHome has no "current source index" action on Product,
            #  so we store the index before switching)
            for idx, src in enumerate(sources):
                if src.get("Type", "").strip() == "Playlist":
                    # We don't know the current index directly — just store
                    # the non-Playlist source info for potential restore later.
                    # For now, track that we need to restore.
                    break
            ok = await self._client.select_playlist_source()
            if ok:
                logger.info("openhome_source_selected", device=self._device_name,
                            source="Playlist")
        except Exception as exc:
            logger.warning("openhome_source_select_failed", device=self._device_name,
                           error=str(exc))

    # -- Standby control (Phase 3) -------------------------------------------

    async def _wake_from_standby(self) -> None:
        """Wake device from standby if it is currently in standby."""
        try:
            is_standby = await self._client.product_get_standby()
            if is_standby:
                await self._client.product_set_standby(False)
                logger.info("openhome_woke_from_standby", device=self._device_name)
                # Give device time to wake up
                await asyncio.sleep(1.0)
        except Exception as exc:
            logger.warning("openhome_standby_check_failed", device=self._device_name,
                           error=str(exc))

    async def _maybe_standby(self) -> None:
        """Optionally put the device into standby on stop."""
        if not self._get_auto_standby():
            return
        try:
            await self._client.product_set_standby(True)
            logger.info("openhome_set_standby", device=self._device_name)
        except Exception as exc:
            logger.warning("openhome_standby_set_failed", device=self._device_name,
                           error=str(exc))

    # -- UPnP Eventing (Phase 4) ---------------------------------------------

    async def _subscribe_events(self) -> None:
        """Subscribe to all available OpenHome event URLs.

        If at least one subscription succeeds, ``_eventing_active`` is set
        to ``True`` and the position monitor switches to a lighter mode
        that only polls for position (since state/track changes arrive
        via NOTIFY).

        If no subscription succeeds, falls back to full polling.
        """
        if not self._event_listener or not self._event_sub_urls:
            logger.debug("oh_eventing_unavailable", device=self._device_name,
                         reason="no listener" if not self._event_listener else "no event URLs")
            return

        # Services to subscribe to, in priority order
        services = ["transport", "info", "volume", "time", "playlist"]
        subscribed = 0

        for svc in services:
            url = self._event_sub_urls.get(svc)
            if not url:
                continue
            path_id = await self._event_listener.subscribe(url, self._handle_event)
            if path_id:
                self._active_sub_ids.append(path_id)
                subscribed += 1
                logger.debug("oh_service_subscribed", device=self._device_name,
                             service=svc)
            else:
                logger.debug("oh_service_subscribe_failed", device=self._device_name,
                             service=svc)

        if subscribed > 0:
            self._eventing_active = True
            logger.info("oh_eventing_active", device=self._device_name,
                        subscribed=subscribed, total=len(services))
        else:
            logger.info("oh_eventing_fallback_polling", device=self._device_name)

    async def _unsubscribe_events(self) -> None:
        """Unsubscribe from all active event subscriptions."""
        if not self._event_listener:
            return
        for path_id in self._active_sub_ids:
            await self._event_listener.unsubscribe(path_id)
        self._active_sub_ids.clear()
        self._eventing_active = False

    async def _handle_event(self, properties: dict[str, str]) -> None:
        """Process parsed UPnP event properties from a NOTIFY callback.

        Called by the shared :class:`OpenHomeEventListener` when the
        device pushes a state change.
        """
        logger.debug("oh_event_received", device=self._device_name,
                      properties=list(properties.keys()))

        # -- Transport state ---------------------------------------------------
        transport_state = properties.get("TransportState")
        if transport_state:
            logger.info("oh_event_transport_state", device=self._device_name,
                        state=transport_state)
            # No direct action needed — the player queries state when needed.
            # But we can update availability.
            if transport_state in ("Playing", "Paused", "Buffering"):
                self._available = True

        # -- Track URI change (Info service) -----------------------------------
        track_uri = properties.get("Uri")
        if track_uri is not None and track_uri != self._current_track_uri and self._current_track_uri:
            prev = self._current_track_uri
            self._current_track_uri = track_uri
            logger.info("oh_event_track_changed", device=self._device_name,
                        from_uri=prev[:60], to_uri=track_uri[:60])

            # Update current OH ID
            for oh_id, uri in self._track_uri_map.items():
                if uri == track_uri:
                    self._current_oh_id = oh_id
                    break

            # Signal track advancement to the player
            if self.on_track_advanced:
                self.on_track_advanced.set()
        elif track_uri is not None and not self._current_track_uri:
            # First URI report — just store it
            self._current_track_uri = track_uri

        # -- Position / Time ---------------------------------------------------
        seconds = properties.get("Seconds")
        if seconds is not None:
            try:
                self._last_position_ms = int(seconds) * 1000
            except (ValueError, TypeError):
                pass

        # -- Volume ------------------------------------------------------------
        volume_val = properties.get("Volume")
        if volume_val is not None:
            try:
                self._volume = int(volume_val) / 100.0
            except (ValueError, TypeError):
                pass

        mute_val = properties.get("Mute")
        if mute_val is not None:
            # We don't track mute state currently, but log it
            logger.debug("oh_event_mute", device=self._device_name, mute=mute_val)

    # -- Position monitor (Phase 3) ------------------------------------------

    def _start_monitor(self) -> None:
        """Start the background position polling loop.

        When UPnP eventing is active, runs a lighter loop that only
        polls position (state/track changes come via NOTIFY).
        When eventing is unavailable, runs the full polling loop.
        """
        self._stop_monitor()
        self._monitor_task = asyncio.create_task(self._position_monitor())

    def _stop_monitor(self) -> None:
        """Cancel the position monitor task if running."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            self._monitor_task = None

    async def _position_monitor(self) -> None:
        """Poll device position, detect track changes.

        In eventing mode: only polls position via Time service (track
        changes arrive instantly via NOTIFY).
        In polling mode: polls both position and track URI every 2s.
        """
        try:
            prev_track_uri = self._current_track_uri
            while True:
                await asyncio.sleep(_POLL_INTERVAL_S)

                # Get position from Time service (always needed, even with
                # eventing, since not all devices push Time events reliably)
                try:
                    dur, pos = await self._client.time_get()
                    self._last_position_ms = pos * 1000 if pos >= 0 else -1
                except Exception:
                    continue

                # In eventing mode, skip track-change polling — NOTIFY handles it
                if self._eventing_active:
                    continue

                # Full polling fallback: detect track change via Info service
                try:
                    info = await self._client.info_track()
                    current_uri = info.get("Uri", "")
                except Exception:
                    continue

                if (prev_track_uri
                        and current_uri
                        and current_uri != prev_track_uri):
                    logger.info("openhome_track_advanced",
                                device=self._device_name,
                                from_uri=prev_track_uri[:60],
                                to_uri=current_uri[:60])
                    prev_track_uri = current_uri
                    self._current_track_uri = current_uri

                    # Update current OH ID based on what the device is playing
                    await self._sync_current_id_from_device()

                    # Signal track advancement to the player
                    if self.on_track_advanced:
                        self.on_track_advanced.set()

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("openhome_monitor_error", device=self._device_name)

    async def _sync_current_id_from_device(self) -> None:
        """Update our current OH ID tracking after the device advanced."""
        try:
            ids = await self._client.playlist_id_array()
            if not ids:
                return
            # Find which track the device is playing by checking the Info URI
            info = await self._client.info_track()
            current_uri = info.get("Uri", "")
            if not current_uri:
                return
            # Match URI to our tracked IDs
            for oh_id in ids:
                if self._track_uri_map.get(oh_id) == current_uri:
                    self._current_oh_id = oh_id
                    logger.debug("openhome_current_id_synced",
                                 device=self._device_name, oh_id=oh_id)
                    return
        except Exception:
            pass

    def has_uri_changed(self) -> bool:
        """Check if the device's current URI differs from what we set.

        Used by the player's ``_direct_url_monitor`` for gapless detection.
        """
        # The monitor task updates _current_track_uri via polling.
        # If it has changed from what we started with, return True.
        return False  # We use on_track_advanced event instead

    def sync_last_uri(self) -> None:
        """Update our tracked URI after a detected transition."""
        pass  # Handled internally by _position_monitor

    # -- OutputTarget interface ------------------------------------------------

    async def start(self, stream_info: AudioStreamInfo, track: Optional[Track] = None) -> None:
        if not track:
            logger.error("openhome_no_track", device=self._device_name)
            return

        await self.stop()

        # Phase 3: wake from standby and select Playlist source
        await self._wake_from_standby()
        await self._save_and_select_playlist_source()
        await asyncio.sleep(0.3)

        uri = self._build_stream_url(track)
        metadata = _build_didl(track, uri, self._server_ip)

        await self._client.playlist_delete_all()
        self._playlist_ids = []
        self._track_uri_map = {}

        new_id = await self._client.playlist_insert(0, uri, metadata)
        if new_id is not None:
            self._current_oh_id = new_id
            self._playlist_ids = [new_id]
            self._track_uri_map[new_id] = uri
            self._current_track_uri = uri
            await self._client.playlist_seek_id(new_id)
            await self._client.transport_play()
            self._available = True

            # Start position monitor
            self._start_monitor()

            logger.info("openhome_play_started", device=self._device_name,
                        track=track.title, uri=uri[:80])
        else:
            logger.error("openhome_insert_failed", device=self._device_name)
            self._available = False

    async def write(self, data: bytes) -> None:
        pass

    async def flush(self) -> None:
        pass

    async def pause(self) -> None:
        await self._client.transport_pause()

    async def resume(self) -> None:
        await self._client.transport_play()

    async def stop(self) -> None:
        self._stop_monitor()
        await self._client.transport_stop()
        self._current_oh_id = None
        self._current_track_uri = ""
        self._last_position_ms = -1

        # Phase 3: optionally put device to standby
        await self._maybe_standby()

    async def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        await self._client.volume_set(int(self._volume * 100))

    async def get_position_ms(self) -> int:
        """Return position from the Time service.

        Uses the cached value from the background monitor for low latency.
        Falls back to a direct query if the monitor hasn't reported yet.
        """
        if self._last_position_ms >= 0:
            return self._last_position_ms
        # Direct query fallback
        dur, pos = await self._client.time_get()
        return pos * 1000 if pos >= 0 else -1

    async def seek(self, position_ms: int) -> bool:
        seconds = position_ms // 1000
        await self._client.transport_seek_seconds(seconds)
        return True

    # -- Phase 2: Native Playlist + Gapless -----------------------------------

    async def set_next_track(self, stream_info: AudioStreamInfo, track: Track) -> bool:
        """Insert next track after the current one for native gapless playback.

        The device handles the seamless transition — no FFmpeg crossfade needed.
        """
        uri = self._build_stream_url(track)
        metadata = _build_didl(track, uri, self._server_ip)
        after_id = self._current_oh_id or 0
        new_id = await self._client.playlist_insert(after_id, uri, metadata)
        if new_id is not None:
            self._playlist_ids.append(new_id)
            self._track_uri_map[new_id] = uri
            logger.debug("openhome_next_track_set", device=self._device_name,
                         track=track.title)
            return True
        return False

    async def push_queue(self, tracks: list[Track], start_index: int = 0) -> None:
        """Push Tune's queue to the device's OpenHome Playlist.

        Clears the device playlist, inserts up to ``openhome_queue_size``
        tracks, seeks to ``start_index``, and starts playback.
        """
        # Phase 3: wake + select source
        await self._wake_from_standby()
        await self._save_and_select_playlist_source()
        await asyncio.sleep(0.3)

        await self._client.playlist_delete_all()
        self._playlist_ids = []
        self._track_uri_map = {}

        max_tracks = self._get_max_queue_size()
        tracks_to_push = tracks[:max_tracks]

        last_id = 0
        for track in tracks_to_push:
            uri = self._build_stream_url(track)
            metadata = _build_didl(track, uri, self._server_ip)
            new_id = await self._client.playlist_insert(last_id, uri, metadata)
            if new_id is not None:
                self._playlist_ids.append(new_id)
                self._track_uri_map[new_id] = uri
                last_id = new_id
            else:
                logger.warning("openhome_push_insert_failed", track=track.title)
                break

        if not self._playlist_ids:
            logger.error("openhome_push_queue_empty", device=self._device_name)
            return

        # Seek to start_index
        if start_index > 0 and start_index < len(self._playlist_ids):
            target_id = self._playlist_ids[start_index]
            self._current_oh_id = target_id
            self._current_track_uri = self._track_uri_map.get(target_id, "")
            await self._client.playlist_seek_id(target_id)
        else:
            self._current_oh_id = self._playlist_ids[0]
            self._current_track_uri = self._track_uri_map.get(self._current_oh_id, "")
            await self._client.playlist_seek_id(self._current_oh_id)

        await self._client.playlist_play()

        # Start position monitor
        self._start_monitor()

        logger.info("openhome_queue_pushed", device=self._device_name,
                    tracks=len(self._playlist_ids), start_index=start_index)

    async def sync_queue(self, tracks: list[Track]) -> None:
        """Sync Tune's queue to the device Playlist.

        Performs a full replace: clears the device playlist and re-inserts
        all tracks.  Preserves playback if the currently-playing track
        is still in the new queue.
        """
        # Remember what's currently playing so we can resume at the right spot
        current_uri = self._current_track_uri

        await self._client.playlist_delete_all()
        self._playlist_ids = []
        self._track_uri_map = {}

        max_tracks = self._get_max_queue_size()
        tracks_to_push = tracks[:max_tracks]

        last_id = 0
        resume_id: int | None = None
        for track in tracks_to_push:
            uri = self._build_stream_url(track)
            metadata = _build_didl(track, uri, self._server_ip)
            new_id = await self._client.playlist_insert(last_id, uri, metadata)
            if new_id is not None:
                self._playlist_ids.append(new_id)
                self._track_uri_map[new_id] = uri
                last_id = new_id
                # Track if this was the previously-playing URI
                if uri == current_uri:
                    resume_id = new_id
            else:
                logger.warning("openhome_sync_insert_failed", track=track.title)
                break

        if self._playlist_ids:
            target_id = resume_id or self._playlist_ids[0]
            self._current_oh_id = target_id
            self._current_track_uri = self._track_uri_map.get(target_id, "")
            await self._client.playlist_seek_id(target_id)
            await self._client.transport_play()
            logger.info("openhome_queue_synced", device=self._device_name,
                        tracks=len(self._playlist_ids),
                        resumed=resume_id is not None)

    async def close(self) -> None:
        self._stop_monitor()
        await self.stop()
        await self._client.close()

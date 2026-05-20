"""Typed dataclasses for high-frequency EventBus events.

These provide compile-time field validation when *constructing* event data,
while remaining 100 % backward-compatible: the EventBus transparently
converts them to plain dicts via ``to_dict()`` before dispatch.

Usage::

    from tune_server.event_types import PlaybackStartedData

    bus.emit_nowait(Event(
        type=EventType.PLAYBACK_STARTED,
        data=PlaybackStartedData(zone_id=1, title="Bohemian Rhapsody"),
    ))
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class PlaybackStartedData:
    zone_id: int
    track_id: int | None = None
    title: str = ""
    artist_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class PlaybackStoppedData:
    zone_id: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class TrackChangedData:
    zone_id: int
    track_id: int | None = None
    title: str = ""
    artist_name: str = ""
    album_title: str = ""
    cover_url: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class VolumeChangedData:
    zone_id: int
    volume: float
    muted: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class DeviceDiscoveredData:
    device_id: str
    name: str
    device_type: str
    host: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ScanProgressData:
    scanned: int
    total: int
    current_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

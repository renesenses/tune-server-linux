"""Party Mode — collaborative playlist where anyone on the network can add tracks."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from tune_server.api.deps import deps

router = APIRouter(prefix="/party", tags=["party"])


class PartyAddRequest(BaseModel):
    query: str  # Search query (title, artist, or album name)
    zone_id: int | None = None  # Target zone (defaults to first playing zone)


class PartyStatus(BaseModel):
    active: bool
    zone_id: int | None
    zone_name: str | None
    current_track: dict | None
    queue_length: int
    guests_can_add: bool


@router.get("/status")
async def party_status():
    """Get party mode status — current track and queue info."""
    if not deps.zone_manager:
        return PartyStatus(active=False, zone_id=None, zone_name=None,
                           current_track=None, queue_length=0, guests_can_add=True)

    # Find the first playing zone, or first zone
    zones = list(deps.zone_manager.zones.values())
    active_zone = None
    for z in zones:
        if z.player.state.value == "playing":
            active_zone = z
            break
    if not active_zone and zones:
        active_zone = zones[0]

    if not active_zone:
        return PartyStatus(active=False, zone_id=None, zone_name=None,
                           current_track=None, queue_length=0, guests_can_add=True)

    track = active_zone.current_track
    return PartyStatus(
        active=True,
        zone_id=active_zone.zone_id,
        zone_name=active_zone.name,
        current_track={
            "title": track.title,
            "artist": track.artist_name,
            "album": track.album_title,
            "cover_path": track.cover_path,
        } if track else None,
        queue_length=active_zone.player.queue.length,
        guests_can_add=True,
    )


@router.post("/add")
async def party_add_track(request: PartyAddRequest):
    """Search and add a track to the party queue."""
    if not deps.zone_manager or not deps.track_repo:
        raise HTTPException(status_code=503, detail="Server not ready")

    # Find target zone
    zone = None
    if request.zone_id:
        zone = deps.zone_manager.get_zone(request.zone_id)
    else:
        for z in deps.zone_manager.zones.values():
            if z.player.state.value == "playing":
                zone = z
                break
        if not zone:
            zones = list(deps.zone_manager.zones.values())
            zone = zones[0] if zones else None

    if not zone:
        raise HTTPException(status_code=404, detail="No zone available")

    # Search for the track
    from tune_server.db.repository import full_text_search
    results = await full_text_search(deps.db, request.query, limit=5)

    if results.tracks:
        track = results.tracks[0]
        zone.player.queue.add_tracks([track])
        from tune_server.event_bus import Event, EventType
        await deps.event_bus.emit(Event(
            type=EventType.PLAYBACK_QUEUE_CHANGED,
            data={"zone_id": zone.zone_id, "party_add": track.title},
            source="party",
        ))
        return {
            "added": True,
            "track": track.title,
            "artist": track.artist_name,
            "position": zone.player.queue.length,
        }

    raise HTTPException(status_code=404, detail=f"No track found for '{request.query}'")


@router.get("/queue")
async def party_queue(zone_id: int | None = None):
    """Get the current party queue."""
    if not deps.zone_manager:
        return []

    zone = None
    if zone_id:
        zone = deps.zone_manager.get_zone(zone_id)
    else:
        for z in deps.zone_manager.zones.values():
            if z.player.state.value == "playing":
                zone = z
                break
        if not zone:
            zones = list(deps.zone_manager.zones.values())
            zone = zones[0] if zones else None

    if not zone:
        return []

    tracks = zone.player.queue.tracks
    pos = zone.player.queue.position
    return [
        {
            "position": i,
            "title": t.title,
            "artist": t.artist_name,
            "album": t.album_title,
            "is_current": i == pos,
        }
        for i, t in enumerate(tracks)
    ]

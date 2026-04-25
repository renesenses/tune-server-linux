"""Party Mode — collaborative playlist where anyone on the network can add tracks."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tune_server.api.deps import deps

logger = structlog.get_logger()

router = APIRouter(prefix="/party", tags=["party"])

# Votes per zone: {zone_id: {queue_position: vote_count}}
_party_votes: dict[int, dict[int, int]] = {}


class PartyAddRequest(BaseModel):
    query: str  # Search query (title, artist, or album name)
    zone_id: int | None = None  # Target zone (defaults to first playing zone)


class PartyVoteRequest(BaseModel):
    position: int  # Queue position to upvote
    zone_id: int | None = None


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
    active_zone = _resolve_zone()

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

    zone = _resolve_zone(request.zone_id)
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


def _resolve_zone(zone_id: int | None = None):
    """Find the target zone — by id, or first playing, or first available."""
    if not deps.zone_manager:
        return None
    if zone_id:
        return deps.zone_manager.get_zone(zone_id)
    for z in deps.zone_manager.zones.values():
        if z.player.state.value == "playing":
            return z
    zones = list(deps.zone_manager.zones.values())
    return zones[0] if zones else None


@router.get("/queue")
async def party_queue(zone_id: int | None = None):
    """Get the current party queue with vote counts.

    Upcoming tracks (after current position) are sorted by votes descending.
    """
    zone = _resolve_zone(zone_id)
    if not zone:
        return []

    tracks = zone.player.queue.tracks
    pos = zone.player.queue.position
    zid = zone.zone_id
    zone_votes = _party_votes.get(zid, {})

    # Build played + current items (preserve order)
    played_and_current = []
    for i in range(min(pos + 1, len(tracks))):
        t = tracks[i]
        played_and_current.append({
            "position": i,
            "title": t.title,
            "artist": t.artist_name,
            "album": t.album_title,
            "is_current": i == pos,
            "votes": zone_votes.get(i, 0),
        })

    # Build upcoming items, sorted by votes descending (stable for equal votes)
    upcoming = []
    for i in range(pos + 1, len(tracks)):
        t = tracks[i]
        upcoming.append({
            "position": i,
            "title": t.title,
            "artist": t.artist_name,
            "album": t.album_title,
            "is_current": False,
            "votes": zone_votes.get(i, 0),
        })
    upcoming.sort(key=lambda x: x["votes"], reverse=True)

    return played_and_current + upcoming


@router.post("/vote")
async def party_vote(request: PartyVoteRequest):
    """Upvote a track in the party queue.

    Increments the vote count for the given position. If the track now has
    more votes than the one directly before it (and it is not the currently
    playing track), the two are swapped in the queue so higher-voted tracks
    bubble up.
    """
    zone = _resolve_zone(request.zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="No zone available")

    zid = zone.zone_id
    pos = request.position
    queue = zone.player.queue
    current_pos = queue.position

    if pos < 0 or pos >= queue.length:
        raise HTTPException(status_code=400, detail="Invalid queue position")
    if pos <= current_pos:
        raise HTTPException(status_code=400, detail="Cannot vote for already-played or current track")

    # Increment vote
    zone_votes = _party_votes.setdefault(zid, {})
    zone_votes[pos] = zone_votes.get(pos, 0) + 1
    logger.info("party_vote", zone_id=zid, position=pos, votes=zone_votes[pos])

    # Bubble up: swap with the track before if it has fewer votes,
    # but never move into or before the current position.
    swap_pos = pos
    while swap_pos - 1 > current_pos:
        prev = swap_pos - 1
        prev_votes = zone_votes.get(prev, 0)
        cur_votes = zone_votes.get(swap_pos, 0)
        if cur_votes > prev_votes:
            # Swap in the actual queue
            queue.move_track(swap_pos, prev)
            # Swap vote counts
            zone_votes[prev], zone_votes[swap_pos] = zone_votes.get(swap_pos, 0), zone_votes.get(prev, 0)
            swap_pos = prev
        else:
            break

    # Emit queue-changed event
    from tune_server.event_bus import Event, EventType
    await deps.event_bus.emit(Event(
        type=EventType.PLAYBACK_QUEUE_CHANGED,
        data={"zone_id": zid, "party_vote": True},
        source="party",
    ))

    return {"position": swap_pos, "votes": zone_votes.get(swap_pos, 0), "zone_votes": zone_votes}


@router.post("/vote/reset")
async def party_reset_votes(zone_id: int | None = None):
    """Clear all vote counts for a zone (call when track changes)."""
    zone = _resolve_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="No zone available")

    zid = zone.zone_id
    had_votes = zid in _party_votes and bool(_party_votes[zid])
    _party_votes.pop(zid, None)
    logger.info("party_votes_reset", zone_id=zid, had_votes=had_votes)
    return {"reset": True, "zone_id": zid}

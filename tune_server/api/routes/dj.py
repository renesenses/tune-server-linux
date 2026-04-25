"""DJ Mode — dual-deck playback with crossfade transitions."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tune_server.api.deps import deps
from tune_server.event_bus import Event, EventType

import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/dj", tags=["dj"])


class DJLoadRequest(BaseModel):
    track_id: int | None = None
    album_id: int | None = None
    source: str | None = None
    source_id: str | None = None


class DJCrossfadeRequest(BaseModel):
    duration_seconds: float = 5.0
    curve: str = "linear"  # linear, equal_power


# In-memory DJ state per zone
_dj_state: dict[int, dict] = {}


def _get_dj(zone_id: int) -> dict:
    if zone_id not in _dj_state:
        _dj_state[zone_id] = {
            "enabled": False,
            "deck_a": None,  # Track
            "deck_b": None,  # Track
            "active_deck": "a",
            "crossfading": False,
            "crossfade_duration": 5.0,
            "auto_crossfade": False,
            "auto_crossfade_before_end": 10,  # seconds before end
        }
    return _dj_state[zone_id]


@router.post("/enable/{zone_id}")
async def enable_dj(zone_id: int):
    """Enable DJ mode on a zone."""
    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(404, "Zone not found")

    dj = _get_dj(zone_id)
    dj["enabled"] = True

    # Load current track as deck A
    track = zone.current_track
    if track:
        dj["deck_a"] = {
            "title": track.title,
            "artist": track.artist_name,
            "album": track.album_title,
            "cover": track.cover_path,
            "duration_ms": track.duration_ms,
        }
        dj["active_deck"] = "a"

    return {"enabled": True, "zone_id": zone_id, "deck_a": dj["deck_a"]}


@router.post("/disable/{zone_id}")
async def disable_dj(zone_id: int):
    """Disable DJ mode."""
    if zone_id in _dj_state:
        del _dj_state[zone_id]
    return {"enabled": False}


@router.post("/load/{zone_id}/{deck}")
async def load_deck(zone_id: int, deck: str, request: DJLoadRequest):
    """Load a track onto deck A or B."""
    if deck not in ("a", "b"):
        raise HTTPException(400, "Deck must be 'a' or 'b'")

    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(404, "Zone not found")

    dj = _get_dj(zone_id)
    if not dj["enabled"]:
        raise HTTPException(400, "DJ mode not enabled")

    # Resolve track
    track = None
    if request.track_id:
        track = await deps.track_repo.get(request.track_id)
    elif request.album_id:
        tracks = await deps.track_repo.list_by_album(request.album_id)
        track = tracks[0] if tracks else None

    if not track:
        raise HTTPException(404, "Track not found")

    deck_info = {
        "title": track.title,
        "artist": track.artist_name,
        "album": track.album_title,
        "cover": track.cover_path,
        "duration_ms": track.duration_ms,
        "track_id": track.id,
    }

    dj[f"deck_{deck}"] = deck_info

    # If this is the inactive deck, preload it
    if dj["active_deck"] != deck:
        # Queue the track for crossfade
        zone.player.queue.add_tracks([track])

    return {"deck": deck, "loaded": deck_info}


@router.post("/crossfade/{zone_id}")
async def start_crossfade(zone_id: int, request: DJCrossfadeRequest = DJCrossfadeRequest()):
    """Start crossfade from active deck to the other deck."""
    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(404, "Zone not found")

    dj = _get_dj(zone_id)
    if not dj["enabled"]:
        raise HTTPException(400, "DJ mode not enabled")

    target_deck = "b" if dj["active_deck"] == "a" else "a"
    target_track = dj.get(f"deck_{target_deck}")

    if not target_track:
        raise HTTPException(400, f"Deck {target_deck} has no track loaded")

    dj["crossfading"] = True
    dj["crossfade_duration"] = request.duration_seconds

    # Apply crossfade via FFmpeg filter
    # Set a volume fade-out on current, then skip to next track
    duration_ms = int(request.duration_seconds * 1000)

    # Use the EQ/filter mechanism to apply fade-out
    fade_filter = f"afade=t=out:st=0:d={request.duration_seconds}"
    zone.player.set_channel_filter(fade_filter)

    # Schedule the transition
    async def _do_crossfade():
        await asyncio.sleep(request.duration_seconds * 0.8)
        # Skip to next track (which should be the deck B track)
        await zone.player.skip_next()
        # Remove fade filter
        zone.player.set_channel_filter(None)
        dj["active_deck"] = target_deck
        dj["crossfading"] = False
        logger.info("dj_crossfade_complete", zone_id=zone_id,
                     from_deck=dj["active_deck"], to_deck=target_deck)

    asyncio.create_task(_do_crossfade())

    return {
        "crossfading": True,
        "from_deck": dj["active_deck"],
        "to_deck": target_deck,
        "duration": request.duration_seconds,
    }


@router.post("/auto-crossfade/{zone_id}")
async def toggle_auto_crossfade(zone_id: int, body: dict = {}):
    """Toggle auto-crossfade (crossfade X seconds before track end)."""
    dj = _get_dj(zone_id)
    dj["auto_crossfade"] = body.get("enabled", not dj["auto_crossfade"])
    if "before_end" in body:
        dj["auto_crossfade_before_end"] = body["before_end"]
    return {"auto_crossfade": dj["auto_crossfade"],
            "before_end": dj["auto_crossfade_before_end"]}


@router.get("/status/{zone_id}")
async def dj_status(zone_id: int):
    """Get DJ mode status for a zone."""
    dj = _get_dj(zone_id)

    zone = deps.zone_manager.get_zone(zone_id) if deps.zone_manager else None
    position_ms = zone.player.position_ms if zone else 0

    return {
        "enabled": dj["enabled"],
        "active_deck": dj["active_deck"],
        "deck_a": dj.get("deck_a"),
        "deck_b": dj.get("deck_b"),
        "crossfading": dj["crossfading"],
        "crossfade_duration": dj["crossfade_duration"],
        "auto_crossfade": dj["auto_crossfade"],
        "position_ms": position_ms,
    }


@router.post("/volume/{zone_id}/{deck}")
async def set_deck_volume(zone_id: int, deck: str, body: dict):
    """Set volume for a specific deck (0.0-1.0)."""
    if deck not in ("a", "b"):
        raise HTTPException(400, "Deck must be 'a' or 'b'")

    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(404, "Zone not found")

    volume = body.get("volume", 1.0)
    # For now, deck volume maps to zone volume (single-output)
    await zone.player.set_volume(volume)
    return {"deck": deck, "volume": volume}

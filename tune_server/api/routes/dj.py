"""DJ Mode — dual-deck playback with PCM mixing and crossfade transitions."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tune_server.api.deps import deps
from tune_server.event_bus import Event, EventType
from tune_server.playback.dj_player import DualDeckPlayer

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


class DJCrossfaderRequest(BaseModel):
    position: float = 0.0  # -1.0 (full A) to 1.0 (full B)


class DJGainRequest(BaseModel):
    gain: float = 1.0  # 0.0 to 1.0


# In-memory DJ state per zone
_dj_state: dict[int, dict] = {}


def _get_dj(zone_id: int) -> dict:
    if zone_id not in _dj_state:
        _dj_state[zone_id] = {
            "enabled": False,
            "player": None,  # DualDeckPlayer
            "auto_crossfade": False,
            "auto_crossfade_before_end": 10,  # seconds before end
        }
    return _dj_state[zone_id]


def _get_dj_player(zone_id: int) -> DualDeckPlayer:
    """Get the DualDeckPlayer for a zone, raising 400 if DJ mode not enabled."""
    dj = _get_dj(zone_id)
    if not dj["enabled"] or not dj["player"]:
        raise HTTPException(400, "DJ mode not enabled")
    return dj["player"]


@router.post("/enable/{zone_id}")
async def enable_dj(zone_id: int):
    """Enable DJ mode on a zone — creates a DualDeckPlayer with PCM mixing."""
    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(404, "Zone not found")

    dj = _get_dj(zone_id)

    # If already enabled, return current state
    if dj["enabled"] and dj["player"]:
        player: DualDeckPlayer = dj["player"]
        return {"enabled": True, "zone_id": zone_id, **player.status()}

    # Stop normal playback before entering DJ mode
    await zone.player.stop()

    # Create the DualDeckPlayer using the zone's output and stream resolver
    output = zone.player._output
    event_bus = deps.event_bus
    stream_url_resolver = zone.player._stream_url_resolver
    capabilities = output.capabilities if output else None

    player = DualDeckPlayer(
        zone_id=zone_id,
        output=output,
        event_bus=event_bus,
        stream_url_resolver=stream_url_resolver,
        capabilities=capabilities,
    )

    dj["enabled"] = True
    dj["player"] = player

    # Load current track as deck A if something was playing
    track = zone.current_track
    deck_a_info = None
    if track:
        try:
            deck_a_info = await player.load_deck("a", track)
            await player.play_deck("a")
        except Exception as exc:
            logger.warning("dj_enable_load_current_failed", error=str(exc))

    logger.info("dj_mode_enabled", zone_id=zone_id)

    return {"enabled": True, "zone_id": zone_id, "deck_a": deck_a_info}


@router.post("/disable/{zone_id}")
async def disable_dj(zone_id: int):
    """Disable DJ mode — stops the DualDeckPlayer and restores normal playback."""
    dj = _get_dj(zone_id)

    if dj.get("player"):
        player: DualDeckPlayer = dj["player"]
        await player.stop()

    dj["enabled"] = False
    dj["player"] = None

    if zone_id in _dj_state:
        del _dj_state[zone_id]

    logger.info("dj_mode_disabled", zone_id=zone_id)
    return {"enabled": False}


@router.post("/load/{zone_id}/{deck}")
async def load_deck(zone_id: int, deck: str, request: DJLoadRequest):
    """Load a track onto deck A or B."""
    if deck not in ("a", "b"):
        raise HTTPException(400, "Deck must be 'a' or 'b'")

    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(404, "Zone not found")

    player = _get_dj_player(zone_id)

    # Resolve track
    track = None
    if request.track_id:
        track = await deps.track_repo.get(request.track_id)
    elif request.album_id:
        tracks = await deps.track_repo.list_by_album(request.album_id)
        track = tracks[0] if tracks else None

    if not track:
        raise HTTPException(404, "Track not found")

    try:
        deck_info = await player.load_deck(deck, track)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))

    return {"deck": deck, "loaded": deck_info}


@router.post("/play/{zone_id}/{deck}")
async def play_deck(zone_id: int, deck: str):
    """Start playback on a loaded deck."""
    if deck not in ("a", "b"):
        raise HTTPException(400, "Deck must be 'a' or 'b'")

    player = _get_dj_player(zone_id)

    try:
        await player.play_deck(deck)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))

    return {"deck": deck, "playing": True}


@router.post("/pause/{zone_id}/{deck}")
async def pause_deck(zone_id: int, deck: str):
    """Pause playback on a deck."""
    if deck not in ("a", "b"):
        raise HTTPException(400, "Deck must be 'a' or 'b'")

    player = _get_dj_player(zone_id)
    await player.pause_deck(deck)

    return {"deck": deck, "playing": False}


@router.post("/crossfade/{zone_id}")
async def start_crossfade(zone_id: int, request: DJCrossfadeRequest = DJCrossfadeRequest()):
    """Start crossfade from active deck to the other deck."""
    player = _get_dj_player(zone_id)

    try:
        await player.start_crossfade(
            duration_seconds=request.duration_seconds,
            curve=request.curve,
        )
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))

    return {
        "crossfading": True,
        "from_deck": "a" if player.active_deck == "a" else "b",
        "to_deck": "b" if player.active_deck == "a" else "a",
        "duration": request.duration_seconds,
        "curve": request.curve,
    }


@router.post("/crossfader/{zone_id}")
async def set_crossfader(zone_id: int, request: DJCrossfaderRequest):
    """Set crossfader position (-1.0 = full A, 0.0 = center, 1.0 = full B)."""
    player = _get_dj_player(zone_id)
    await player.set_crossfader(request.position)

    return {
        "position": request.position,
        "gain_a": round(player.gain_a, 3),
        "gain_b": round(player.gain_b, 3),
    }


@router.post("/auto-crossfade/{zone_id}")
async def toggle_auto_crossfade(zone_id: int, body: dict = {}):
    """Toggle auto-crossfade (crossfade X seconds before track end)."""
    dj = _get_dj(zone_id)
    dj["auto_crossfade"] = body.get("enabled", not dj["auto_crossfade"])
    if "before_end" in body:
        dj["auto_crossfade_before_end"] = body["before_end"]
    return {
        "auto_crossfade": dj["auto_crossfade"],
        "before_end": dj["auto_crossfade_before_end"],
    }


@router.get("/status/{zone_id}")
async def dj_status(zone_id: int):
    """Get DJ mode status for a zone."""
    dj = _get_dj(zone_id)

    if dj["enabled"] and dj["player"]:
        player: DualDeckPlayer = dj["player"]
        return {
            "enabled": True,
            **player.status(),
            "auto_crossfade": dj["auto_crossfade"],
            "auto_crossfade_before_end": dj["auto_crossfade_before_end"],
        }

    return {
        "enabled": False,
        "active_deck": "a",
        "deck_a": None,
        "deck_b": None,
        "crossfading": False,
        "gain_a": 1.0,
        "gain_b": 0.0,
        "mixing": False,
        "auto_crossfade": dj.get("auto_crossfade", False),
        "auto_crossfade_before_end": dj.get("auto_crossfade_before_end", 10),
    }


@router.post("/volume/{zone_id}/{deck}")
async def set_deck_volume(zone_id: int, deck: str, body: dict):
    """Set gain for a specific deck (0.0-1.0)."""
    if deck not in ("a", "b"):
        raise HTTPException(400, "Deck must be 'a' or 'b'")

    player = _get_dj_player(zone_id)
    gain = max(0.0, min(1.0, float(body.get("volume", 1.0))))
    await player.set_deck_gain(deck, gain)

    return {"deck": deck, "gain": gain}

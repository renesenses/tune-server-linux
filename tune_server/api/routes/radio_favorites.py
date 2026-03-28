"""Radio favorites API — save/list/delete tracks heard on radio."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from tune_server.api.deps import deps
from tune_server.models import RadioFavorite, RadioFavoriteCreate

router = APIRouter(prefix="/radio-favorites", tags=["radio-favorites"])


@router.get("", response_model=list[RadioFavorite])
async def list_favorites(limit: int = 200, offset: int = 0):
    return await deps.radio_fav_repo.list(limit=limit, offset=offset)


@router.get("/count")
async def count_favorites():
    return {"count": await deps.radio_fav_repo.count()}


@router.post("", response_model=RadioFavorite | None)
async def save_favorite(body: RadioFavoriteCreate):
    """Save the currently playing radio track as a favorite."""
    result = await deps.radio_fav_repo.save(
        title=body.title,
        artist=body.artist,
        station_name=body.station_name,
        cover_url=body.cover_url,
        stream_url=body.stream_url,
    )
    return result


@router.post("/save-current")
async def save_current():
    """Save the currently playing radio track on the current zone."""
    zone_mgr = deps.zone_manager
    if not zone_mgr:
        raise HTTPException(503, "Zone manager not available")

    # Find a playing radio zone
    for zone in zone_mgr.list_zones():
        track = zone.player.current_track
        if track and track.source and track.source.value == "radio":
            result = await deps.radio_fav_repo.save(
                title=track.title or "",
                artist=track.artist_name or "",
                station_name=track.album_title or "",
                cover_url=track.cover_path,
                stream_url=track.file_path,
            )
            is_fav = result is not None
            return {"saved": is_fav, "favorite": result}

    raise HTTPException(400, "No radio currently playing")


@router.get("/is-favorite")
async def is_favorite(title: str, artist: str = ""):
    return {"is_favorite": await deps.radio_fav_repo.is_favorite(title, artist)}


@router.delete("/{fav_id}")
async def delete_favorite(fav_id: int):
    await deps.radio_fav_repo.delete(fav_id)
    return {"deleted": fav_id}


@router.delete("")
async def clear_favorites():
    await deps.radio_fav_repo.clear()
    return {"cleared": True}


@router.get("/export")
async def export_csv():
    """Export favorites as CSV (Soundiiz-compatible)."""
    csv = await deps.radio_fav_repo.export_csv()
    return PlainTextResponse(
        csv,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=radio_favorites.csv"},
    )

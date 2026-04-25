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
    if not deps.radio_fav_repo:
        return {"is_favorite": False}
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


@router.post("/create-playlist")
async def create_playlist_from_favorites(body: dict):
    """Convert radio favorites into a streaming playlist (Tidal/Qobuz/Spotify).

    body: { "service": "tidal", "playlist_name": "Mes favoris radio", "limit": 200 }
    """
    import re

    service_name = body.get("service", "tidal")
    playlist_name = body.get("playlist_name", "Mes favoris radio")
    limit = body.get("limit", 200)

    service = deps.streaming_services.get(service_name)
    if not service:
        raise HTTPException(400, f"Service '{service_name}' not available")
    if not service.is_authenticated:
        raise HTTPException(401, f"Service '{service_name}' not authenticated")
    if not service.supports_playlist_write:
        raise HTTPException(400, f"Service '{service_name}' does not support playlist creation")

    # Fetch favorites
    favorites = await deps.radio_fav_repo.list(limit=limit)
    if not favorites:
        raise HTTPException(404, "No radio favorites found")

    def _normalize(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r'\(.*?\)', '', s)
        s = re.sub(r'\[.*?\]', '', s)
        s = re.sub(r'\s*-\s*(feat|ft|featuring|remix|remaster|deluxe|bonus).*', '', s, flags=re.IGNORECASE)
        if s.startswith("the "):
            s = s[4:]
        return s.strip()

    # Search and match each favorite
    matched_ids = []
    results = {"matched": 0, "approximate": 0, "not_found": 0, "total": len(favorites), "tracks": []}

    for fav in favorites:
        title = fav.title if hasattr(fav, 'title') else fav.get('title', '')
        artist = fav.artist if hasattr(fav, 'artist') else fav.get('artist', '')
        if not title:
            continue

        query = f"{artist} {title}".strip()
        try:
            search_results = await service.search(query, limit=3)
            best_match = None
            match_quality = None

            for track in search_results.tracks:
                t1 = _normalize(title)
                t2 = _normalize(track.title)
                a1 = _normalize(artist)
                a2 = _normalize(track.artist_name or "")

                if t1 == t2 and a1 == a2:
                    best_match = track
                    match_quality = "exact"
                    break
                elif t1 == t2 or (a1 == a2 and (t1 in t2 or t2 in t1)):
                    if not best_match:
                        best_match = track
                        match_quality = "approximate"

            if best_match and best_match.source_id:
                matched_ids.append(best_match.source_id)
                if match_quality == "exact":
                    results["matched"] += 1
                else:
                    results["approximate"] += 1
                results["tracks"].append({
                    "title": title, "artist": artist,
                    "matched_title": best_match.title,
                    "matched_artist": best_match.artist_name,
                    "quality": match_quality,
                })
            else:
                results["not_found"] += 1
                results["tracks"].append({
                    "title": title, "artist": artist,
                    "quality": "not_found",
                })
        except Exception:
            results["not_found"] += 1

    # Create playlist and add tracks
    if matched_ids:
        try:
            playlist_id = await service.create_playlist(playlist_name, f"Created from {len(matched_ids)} radio favorites")
            if playlist_id:
                added = await service.add_tracks_to_playlist(playlist_id, matched_ids)
                results["playlist_id"] = playlist_id
                results["playlist_name"] = playlist_name
                results["service"] = service_name
                results["tracks_added"] = added
        except Exception as e:
            results["error"] = str(e)

    return results

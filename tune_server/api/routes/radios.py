from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, UploadFile

from tune_server.api.deps import deps
from tune_server.event_bus import Event, EventType
from tune_server.library.artwork import save_artwork
from tune_server.models import (
    AudioFormat,
    RadioImportResult,
    RadioStation,
    RadioStationCreate,
    RadioStationUpdate,
    Source,
    Track,
    Zone,
)
from tune_server.utils.playlist_parser import parse_m3u, parse_pls

router = APIRouter(prefix="/radios", tags=["radios"])


def _radio_to_track(station: RadioStation) -> Track:
    """Convert a RadioStation to a Track for playback."""
    # Detect format from codec field or URL
    fmt = AudioFormat.AAC  # default
    codec = (station.codec or "").lower()
    url_lower = station.stream_url.lower()
    if codec in ("mp3",) or url_lower.endswith(".mp3"):
        fmt = AudioFormat.MP3
    elif codec in ("flac",) or url_lower.endswith(".flac"):
        fmt = AudioFormat.FLAC
    elif codec in ("ogg", "opus") or url_lower.endswith(".ogg"):
        fmt = AudioFormat.OGG
    elif codec in ("aac",) or ".aac" in url_lower:
        fmt = AudioFormat.AAC

    return Track(
        id=None,
        title=station.name,
        file_path=station.stream_url,
        format=fmt,
        duration_ms=0,
        source=Source.RADIO,
        source_id=str(station.id),
        cover_path=station.logo_url,
        artist_name=station.genre or "Radio",
        album_title="Live Radio",
    )


@router.post("", response_model=RadioStation, status_code=201)
async def create_radio(station: RadioStationCreate):
    if not deps.radio_repo:
        raise HTTPException(status_code=503, detail="Radio repository not available")
    station_id = await deps.radio_repo.create(station)
    created = await deps.radio_repo.get(station_id)
    if deps.event_bus:
        deps.event_bus.emit_nowait(Event(
            type=EventType.RADIO_CREATED,
            data={"id": station_id, "name": station.name},
            source="api",
        ))
    return created


@router.get("", response_model=list[RadioStation])
async def list_radios(
    genre: str | None = None,
    favorite: bool | None = None,
    limit: int = 100,
    offset: int = 0,
):
    if not deps.radio_repo:
        raise HTTPException(status_code=503, detail="Radio repository not available")
    return await deps.radio_repo.list(limit=limit, offset=offset, genre=genre, favorite=favorite)


@router.get("/{radio_id}", response_model=RadioStation)
async def get_radio(radio_id: int):
    if not deps.radio_repo:
        raise HTTPException(status_code=503, detail="Radio repository not available")
    station = await deps.radio_repo.get(radio_id)
    if not station:
        raise HTTPException(status_code=404, detail="Radio station not found")
    return station


@router.put("/{radio_id}", response_model=RadioStation)
async def update_radio(radio_id: int, update: RadioStationUpdate):
    if not deps.radio_repo:
        raise HTTPException(status_code=503, detail="Radio repository not available")
    existing = await deps.radio_repo.get(radio_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Radio station not found")
    fields = update.model_dump(exclude_unset=True)
    if fields:
        await deps.radio_repo.update(radio_id, **fields)
    updated = await deps.radio_repo.get(radio_id)
    if deps.event_bus:
        deps.event_bus.emit_nowait(Event(
            type=EventType.RADIO_UPDATED,
            data={"id": radio_id},
            source="api",
        ))
    return updated


@router.post("/{radio_id}/artwork", response_model=RadioStation)
async def upload_radio_artwork(radio_id: int, file: UploadFile):
    if not deps.radio_repo:
        raise HTTPException(status_code=503, detail="Radio repository not available")
    station = await deps.radio_repo.get(radio_id)
    if not station:
        raise HTTPException(status_code=404, detail="Radio station not found")

    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    image_data = await file.read()
    if not image_data:
        raise HTTPException(status_code=400, detail="Empty file")

    cover_path = await asyncio.to_thread(
        save_artwork, f"upload:radio:{radio_id}", image_data, True,
    )
    if not cover_path:
        raise HTTPException(status_code=500, detail="Failed to save artwork")

    await deps.radio_repo.update(radio_id, logo_url=cover_path)
    if deps.event_bus:
        deps.event_bus.emit_nowait(Event(
            type=EventType.RADIO_UPDATED,
            data={"id": radio_id},
            source="api",
        ))
    return await deps.radio_repo.get(radio_id)


@router.delete("/{radio_id}", status_code=204)
async def delete_radio(radio_id: int):
    if not deps.radio_repo:
        raise HTTPException(status_code=503, detail="Radio repository not available")
    existing = await deps.radio_repo.get(radio_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Radio station not found")
    await deps.radio_repo.delete(radio_id)
    if deps.event_bus:
        deps.event_bus.emit_nowait(Event(
            type=EventType.RADIO_DELETED,
            data={"id": radio_id, "name": existing.name},
            source="api",
        ))


@router.post("/import", response_model=RadioImportResult)
async def import_radios(file: UploadFile):
    if not deps.radio_repo:
        raise HTTPException(status_code=503, detail="Radio repository not available")

    content = (await file.read()).decode("utf-8", errors="replace")
    filename = (file.filename or "").lower()

    if filename.endswith(".pls"):
        entries = parse_pls(content)
    else:
        entries = parse_m3u(content)

    imported = 0
    skipped = 0
    errors: list[str] = []

    for entry in entries:
        try:
            existing = await deps.radio_repo.get_by_url(entry["url"])
            if existing:
                skipped += 1
                continue
            await deps.radio_repo.create(RadioStationCreate(
                name=entry["name"],
                stream_url=entry["url"],
            ))
            imported += 1
        except Exception as e:
            errors.append(f"{entry.get('name', '?')}: {e}")

    return RadioImportResult(imported=imported, skipped=skipped, errors=errors)


@router.post("/{radio_id}/play/{zone_id}", response_model=Zone)
async def play_radio(radio_id: int, zone_id: int):
    if not deps.radio_repo:
        raise HTTPException(status_code=503, detail="Radio repository not available")
    if not deps.zone_manager:
        raise HTTPException(status_code=503, detail="Zone manager not available")

    station = await deps.radio_repo.get(radio_id)
    if not station:
        raise HTTPException(status_code=404, detail="Radio station not found")

    zone = deps.zone_manager.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    track = _radio_to_track(station)

    group = deps.group_manager.get_group_for_zone(zone_id) if deps.group_manager else None
    if group:
        await group.play([track])
    else:
        await zone.player.play(tracks=[track])

    return zone.to_model()

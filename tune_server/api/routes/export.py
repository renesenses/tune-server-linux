from __future__ import annotations

import csv
import io

import structlog
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from tune_server.api.deps import deps

logger = structlog.get_logger()

router = APIRouter(prefix="/export", tags=["export"])

BOM = "﻿"


def _val(v) -> str:
    if v is None:
        return ""
    return str(v)


def _duration_fmt(ms) -> str:
    if not ms:
        return ""
    total_seconds = int(ms) // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes}:{seconds:02d}"


def _csv_response(buf: io.StringIO, filename: str) -> StreamingResponse:
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/albums.csv")
async def export_albums():
    total = await deps.album_repo.count()
    albums = await deps.album_repo.list(limit=total, offset=0)

    buf = io.StringIO()
    buf.write(BOM)
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "id", "title", "artist_name", "year", "original_year",
        "release_date", "original_date", "genre", "track_count",
        "disc_count", "format", "sample_rate", "bit_depth", "label",
        "catalog_number", "musicbrainz_release_id", "source",
    ])
    for a in albums:
        writer.writerow([
            _val(a.id), _val(a.title), _val(a.artist_name), _val(a.year),
            _val(a.original_year), _val(a.release_date), _val(a.original_date),
            _val(a.genre), _val(a.track_count), _val(a.disc_count),
            _val(a.format), _val(a.sample_rate), _val(a.bit_depth),
            _val(a.label), _val(a.catalog_number),
            _val(a.musicbrainz_release_id), _val(a.source),
        ])

    return _csv_response(buf, "albums.csv")


@router.get("/tracks.csv")
async def export_tracks():
    total = await deps.track_repo.count()
    tracks = await deps.track_repo.list(limit=total, offset=0)

    buf = io.StringIO()
    buf.write(BOM)
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "id", "title", "artist_name", "album_title", "track_number",
        "disc_number", "disc_subtitle", "duration", "duration_ms",
        "format", "sample_rate", "bit_depth", "channels", "file_path",
        "source", "musicbrainz_recording_id",
    ])
    for t in tracks:
        writer.writerow([
            _val(t.id), _val(t.title), _val(t.artist_name),
            _val(t.album_title), _val(t.track_number), _val(t.disc_number),
            _val(t.disc_subtitle), _duration_fmt(t.duration_ms),
            _val(t.duration_ms), _val(t.format), _val(t.sample_rate),
            _val(t.bit_depth), _val(t.channels), _val(t.file_path),
            _val(t.source), _val(t.musicbrainz_recording_id),
        ])

    return _csv_response(buf, "tracks.csv")


@router.get("/artists.csv")
async def export_artists():
    total = await deps.artist_repo.count()
    artists = await deps.artist_repo.list(limit=total, offset=0)

    buf = io.StringIO()
    buf.write(BOM)
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "id", "name", "sort_name", "musicbrainz_id", "bio", "image_path",
    ])
    for a in artists:
        writer.writerow([
            _val(a.id), _val(a.name), _val(a.sort_name),
            _val(a.musicbrainz_id), _val(a.bio), _val(a.image_path),
        ])

    return _csv_response(buf, "artists.csv")

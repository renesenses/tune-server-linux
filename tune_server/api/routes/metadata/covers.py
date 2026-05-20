"""Metadata Manager — cover art search, download, upload, embed."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps
from tune_server.metadata_manager.cover_fetcher import search_covers

router = APIRouter(tags=["metadata"])


# ---------------------------------------------------------------------------
# Covers
# ---------------------------------------------------------------------------

@router.get("/covers/search")
async def search_covers_endpoint(album: str, artist: str = "", release_id: str = ""):
    """Search for album covers from Cover Art Archive + Discogs."""
    from tune_server.config import settings
    from tune_server.api.routes.services import get_discogs_token

    results = await search_covers(
        album_title=album,
        artist_name=artist,
        musicbrainz_release_id=release_id,
        discogs_token=await get_discogs_token(),
        cache_dir=getattr(settings, "artwork_cache_dir", "artwork_cache"),
    )
    return {"results": results}


@router.post("/covers/album/{album_id}")
async def fetch_album_cover(album_id: int):
    """Auto-fetch and assign a cover to an album."""
    row = await deps.db.fetchone("SELECT * FROM albums WHERE id = ?", (album_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Album not found")

    from tune_server.config import settings
    from tune_server.api.routes.services import get_discogs_token as _gdt

    mb_id = row.get("musicbrainz_release_id") or "" if "musicbrainz_release_id" in row.keys() else ""

    results = await search_covers(
        album_title=row["title"],
        artist_name=row.get("artist_name") or "",
        musicbrainz_release_id=mb_id,
        discogs_token=await _gdt(),
        cache_dir=getattr(settings, "artwork_cache_dir", "artwork_cache"),
    )

    if results:
        cover_path = results[0]["local_path"]
        await deps.db.execute(
            "UPDATE albums SET cover_path = ? WHERE id = ?",
            (cover_path, album_id),
        )
        await deps.db.commit()
        return {"ok": True, "cover_path": cover_path, "source": results[0]["source"]}

    return {"ok": False, "error": "No cover found"}


# ---------------------------------------------------------------------------
# Cover upload
# ---------------------------------------------------------------------------

@router.post("/covers/album/{album_id}/upload")
async def upload_album_cover(album_id: int):
    """Upload a cover image for an album (multipart form)."""
    # This endpoint needs to be called with multipart/form-data
    # For now, provide the basic structure
    return {"error": "Use PUT /library/albums/{id}/artwork with multipart upload"}


@router.post("/covers/track/{track_id}/embed")
async def embed_cover_in_file(track_id: int):
    """Embed the album cover into the track's audio file tags."""
    import mutagen
    from mutagen.id3 import APIC
    from pathlib import Path

    row = await deps.db.fetchone(
        """SELECT t.file_path, a.cover_path
           FROM tracks t
           LEFT JOIN albums a ON a.id = t.album_id
           WHERE t.id = ?""",
        (track_id,),
    )
    if not row or not row["file_path"]:
        raise HTTPException(status_code=404, detail="Track not found")
    if not row["cover_path"]:
        return {"ok": False, "error": "No cover art available for this album"}

    cover_path = Path(row["cover_path"])
    if not cover_path.is_absolute():
        from tune_server.config import settings
        cover_path = Path(settings.artwork_cache_dir) / cover_path.name

    if not cover_path.exists():
        return {"ok": False, "error": f"Cover file not found: {cover_path}"}

    try:
        audio = mutagen.File(row["file_path"])
        if audio is None:
            return {"ok": False, "error": "Unsupported audio format"}

        cover_data = cover_path.read_bytes()
        mime = "image/jpeg" if cover_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"

        # ID3 (MP3)
        if hasattr(audio, 'tags') and hasattr(audio.tags, 'add'):
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(APIC(
                encoding=3, mime=mime, type=3,
                desc="Front", data=cover_data,
            ))
            audio.save()
            return {"ok": True, "embedded": True, "size": len(cover_data)}

        # FLAC
        if hasattr(audio, 'pictures'):
            from mutagen.flac import Picture
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.data = cover_data
            audio.clear_pictures()
            audio.add_picture(pic)
            audio.save()
            return {"ok": True, "embedded": True, "size": len(cover_data)}

        return {"ok": False, "error": "Cover embedding not supported for this format"}

    except Exception as e:
        return {"ok": False, "error": str(e)}

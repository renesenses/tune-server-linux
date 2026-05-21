"""Enrich track credits with instruments from MusicBrainz recording relationships."""

from __future__ import annotations

import asyncio

import aiohttp
import structlog

from tune_server.db.sa_repository import AlbumRepo, TrackCreditRepo, TrackRepo

logger = structlog.get_logger()

MB_API = "https://musicbrainz.org/ws/2"
MB_UA = "TuneServer/0.7.2 (https://github.com/renesenses/tune-server-linux)"
MB_RATE_LIMIT = 1.1  # slightly over 1s to stay safe


async def _mb_lookup_recording(session: aiohttp.ClientSession, title: str, artist: str) -> str | None:
    """Search MusicBrainz for a recording and return its MBID."""
    query = f'recording:"{title}" AND artist:"{artist}"'
    try:
        async with session.get(
            f"{MB_API}/recording",
            params={"query": query, "limit": 3, "fmt": "json"},
            headers={"User-Agent": MB_UA},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None

    for rec in data.get("recordings", []):
        rec_title = rec.get("title", "").lower()
        if rec_title == title.lower() or title.lower() in rec_title:
            return rec.get("id")
    return data.get("recordings", [{}])[0].get("id") if data.get("recordings") else None


async def _mb_get_artist_instrument(session: aiohttp.ClientSession, artist_name: str) -> str | None:
    """Look up an artist on MusicBrainz and return their primary instrument."""
    try:
        async with session.get(
            f"{MB_API}/artist",
            params={"query": f'artist:"{artist_name}"', "limit": 3, "fmt": "json"},
            headers={"User-Agent": MB_UA},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None

    for artist in data.get("artists", []):
        if artist.get("name", "").lower() != artist_name.lower():
            continue
        # Check disambiguation (e.g. "US bassist", "jazz trumpeter")
        disamb = artist.get("disambiguation", "").lower()
        for instrument in ["guitar", "bass", "drums", "piano", "keyboard", "trumpet",
                           "saxophone", "violin", "cello", "flute", "harmonica", "organ",
                           "percussion", "trombone", "clarinet", "harp", "banjo", "mandolin",
                           "accordion", "synthesizer", "vibraphone", "oboe", "bassoon"]:
            if instrument in disamb:
                return instrument

        # Check artist-rels for member-of-band with instrument attributes
        artist_id = artist.get("id")
        if artist_id:
            await asyncio.sleep(MB_RATE_LIMIT)
            try:
                async with session.get(
                    f"{MB_API}/artist/{artist_id}",
                    params={"inc": "artist-rels", "fmt": "json"},
                    headers={"User-Agent": MB_UA},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp2:
                    if resp2.status == 200:
                        detail = await resp2.json()
                        for rel in detail.get("relations", []):
                            attrs = rel.get("attributes", [])
                            if rel.get("type") == "member of band" and attrs:
                                for attr in attrs:
                                    if attr not in ("original", "current", "past"):
                                        return attr
            except Exception:
                pass

        # Check for vocalist
        if any(w in disamb for w in ["singer", "vocalist", "vocal"]):
            return "vocals"

        return None

    return None


async def _mb_get_recording_credits(session: aiohttp.ClientSession, recording_id: str) -> list[dict]:
    """Fetch artist relationships for a recording from MusicBrainz.

    Returns list of {name, role, instrument}.
    """
    try:
        async with session.get(
            f"{MB_API}/recording/{recording_id}",
            params={"inc": "artist-rels", "fmt": "json"},
            headers={"User-Agent": MB_UA},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
    except Exception:
        return []

    credits = []
    for rel in data.get("relations", []):
        if rel.get("type") == "instrument":
            artist_name = rel.get("artist", {}).get("name", "")
            attributes = rel.get("attributes", [])
            instrument = ", ".join(attributes) if attributes else None
            if artist_name:
                credits.append({"name": artist_name, "role": "performer", "instrument": instrument})
        elif rel.get("type") == "vocal":
            artist_name = rel.get("artist", {}).get("name", "")
            attributes = rel.get("attributes", [])
            vocal_type = ", ".join(attributes) if attributes else "vocals"
            if artist_name:
                credits.append({"name": artist_name, "role": "performer", "instrument": vocal_type})
        elif rel.get("type") == "performer":
            artist_name = rel.get("artist", {}).get("name", "")
            attributes = rel.get("attributes", [])
            instrument = ", ".join(attributes) if attributes else None
            if artist_name:
                credits.append({"name": artist_name, "role": "performer", "instrument": instrument})
        elif rel.get("type") in ("producer", "engineer", "mix", "conductor"):
            artist_name = rel.get("artist", {}).get("name", "")
            if artist_name:
                credits.append({"name": artist_name, "role": rel["type"], "instrument": None})

    return credits


async def _get_track_artist(track, track_repo: TrackRepo, album_repo: AlbumRepo | None = None) -> str:
    """Get the best artist name for a track, falling back to album artist."""
    artist = ""
    if isinstance(track, dict):
        artist = track.get("artist_name") or ""
        album_id = track.get("album_id")
    else:
        artist = getattr(track, "artist_name", "") or ""
        album_id = getattr(track, "album_id", None)

    if not artist and album_id and album_repo:
        album = await album_repo.get(album_id)
        if album:
            artist = (album.get("artist_name") if isinstance(album, dict) else getattr(album, "artist_name", "")) or ""

    return artist


async def enrich_track_credits(
    track_id: int,
    track_repo: TrackRepo,
    credit_repo: TrackCreditRepo,
    album_repo: AlbumRepo | None = None,
) -> dict:
    """Enrich a single track's credits from MusicBrainz.

    Returns {enriched: int, source: "musicbrainz"} or {enriched: 0, reason: "..."}.
    """
    track = await track_repo.get(track_id)
    if not track:
        return {"enriched": 0, "reason": "track_not_found"}

    existing = await credit_repo.list_by_track(track_id)

    async with aiohttp.ClientSession() as session:
        # Lookup recording
        recording_id = track.get("musicbrainz_recording_id") if hasattr(track, "get") else getattr(track, "musicbrainz_recording_id", None)
        if not recording_id:
            title = track["title"] if isinstance(track, dict) else track.title
            artist = await _get_track_artist(track, track_repo, album_repo)
            recording_id = await _mb_lookup_recording(session, title, artist)
            if not recording_id:
                return {"enriched": 0, "reason": "not_found_on_musicbrainz"}

        await asyncio.sleep(MB_RATE_LIMIT)

        # Get credits from MusicBrainz
        mb_credits = await _mb_get_recording_credits(session, recording_id)
        if not mb_credits:
            return {"enriched": 0, "reason": "no_credits_on_musicbrainz"}

    # Match MB credits to existing credits and update instruments
    enriched = 0
    existing_names = {c.artist_name.lower() for c in existing}

    for mb_credit in mb_credits:
        mb_name = mb_credit["name"].lower()
        mb_instrument = mb_credit.get("instrument")

        # Try to match with existing credit and update instrument
        matched = False
        if mb_instrument:
            for existing_credit in existing:
                if existing_credit.artist_name.lower() == mb_name and not existing_credit.instrument:
                    await credit_repo.update_instrument(existing_credit.id, mb_instrument)
                    enriched += 1
                    matched = True
                    break

        # Add missing credits from MusicBrainz
        if not matched and mb_name not in existing_names and mb_instrument:
            from tune_server.models import TrackCredit as TrackCreditModel
            new_credit = TrackCreditModel(
                track_id=track_id,
                artist_name=mb_credit["name"],
                role=mb_credit.get("role", "performer"),
                instrument=mb_instrument,
                position=len(existing) + enriched,
            )
            await credit_repo.add(new_credit)
            existing_names.add(mb_name)
            enriched += 1

    return {"enriched": enriched, "total_mb_credits": len(mb_credits), "source": "musicbrainz", "recording_id": recording_id}


async def enrich_album_credits(
    album_id: int,
    track_repo: TrackRepo,
    album_repo: AlbumRepo,
    credit_repo: TrackCreditRepo,
) -> dict:
    """Enrich all tracks in an album from MusicBrainz.

    Rate-limited to 1 request/second.
    """
    tracks = await track_repo.list_by_album(album_id)
    if not tracks:
        return {"enriched": 0, "tracks": 0, "reason": "no_tracks"}

    total_enriched = 0
    errors = 0

    async with aiohttp.ClientSession() as session:
        for track in tracks:
            track_id = track["id"] if isinstance(track, dict) else track.id
            title = track["title"] if isinstance(track, dict) else track.title
            artist = await _get_track_artist(track, track_repo, album_repo)

            try:
                # Lookup recording
                recording_id = await _mb_lookup_recording(session, title, artist)
                if not recording_id:
                    continue

                await asyncio.sleep(MB_RATE_LIMIT)

                # Get MB credits
                mb_credits = await _mb_get_recording_credits(session, recording_id)
                if not mb_credits:
                    continue

                await asyncio.sleep(MB_RATE_LIMIT)

                # Match and update
                existing = await credit_repo.list_by_track(track_id)
                existing_names = {c.artist_name.lower() for c in existing}
                for mb_credit in mb_credits:
                    mb_name = mb_credit["name"].lower()
                    mb_instrument = mb_credit.get("instrument")
                    if not mb_instrument:
                        continue

                    matched = False
                    for ex in existing:
                        if ex.artist_name.lower() == mb_name and not ex.instrument:
                            await credit_repo.update_instrument(ex.id, mb_instrument)
                            total_enriched += 1
                            matched = True
                            break

                    if not matched and mb_name not in existing_names:
                        from tune_server.models import TrackCredit as TrackCreditModel
                        new_credit = TrackCreditModel(
                            track_id=track_id,
                            artist_name=mb_credit["name"],
                            role=mb_credit.get("role", "performer"),
                            instrument=mb_instrument,
                            position=len(existing) + total_enriched,
                        )
                        await credit_repo.add(new_credit)
                        existing_names.add(mb_name)
                        total_enriched += 1

            except Exception:
                logger.debug("credit_enrich_track_error", track_id=track_id)
                errors += 1

    logger.info("album_credits_enriched", album_id=album_id, enriched=total_enriched, tracks=len(tracks), errors=errors)
    return {"enriched": total_enriched, "tracks": len(tracks), "errors": errors, "source": "musicbrainz"}


async def enrich_credits_instruments(
    credit_repo: TrackCreditRepo,
    album_id: int | None = None,
    track_repo: TrackRepo | None = None,
) -> dict:
    """Enrich existing credits that have no instrument by looking up each artist on MusicBrainz.

    If album_id is provided, only enriches credits for tracks in that album.
    Otherwise enriches all credits without instruments.
    """
    if album_id and track_repo:
        tracks = await track_repo.list_by_album(album_id)
        track_ids = [t["id"] if isinstance(t, dict) else t.id for t in tracks]
    else:
        track_ids = None

    # Get all credits without instruments
    from tune_server.db.engine import Database
    db = credit_repo._db
    if track_ids:
        placeholders = ",".join("?" * len(track_ids))
        rows = await db.fetchall(
            f"SELECT DISTINCT artist_name FROM track_credits WHERE instrument IS NULL AND track_id IN ({placeholders})",
            tuple(track_ids),
        )
    else:
        rows = await db.fetchall(
            "SELECT DISTINCT artist_name FROM track_credits WHERE instrument IS NULL"
        )

    artist_names = [r["artist_name"] for r in rows]
    if not artist_names:
        return {"enriched": 0, "artists_checked": 0, "reason": "no_credits_without_instruments"}

    enriched = 0
    checked = 0
    instrument_cache: dict[str, str | None] = {}

    async with aiohttp.ClientSession() as session:
        for name in artist_names:
            if name in instrument_cache:
                instrument = instrument_cache[name]
            else:
                instrument = await _mb_get_artist_instrument(session, name)
                instrument_cache[name] = instrument
                checked += 1
                await asyncio.sleep(MB_RATE_LIMIT)

            if instrument:
                # Update all credits for this artist that lack instrument
                if track_ids:
                    placeholders = ",".join("?" * len(track_ids))
                    result = await db.execute(
                        f"UPDATE track_credits SET instrument = ? WHERE artist_name = ? AND instrument IS NULL AND track_id IN ({placeholders})",
                        (instrument, name, *track_ids),
                    )
                else:
                    result = await db.execute(
                        "UPDATE track_credits SET instrument = ? WHERE artist_name = ? AND instrument IS NULL",
                        (instrument, name),
                    )
                await db.commit()
                enriched += result.rowcount
                logger.info("credit_instrument_found", artist=name, instrument=instrument, updated=result.rowcount)

    logger.info("credits_instruments_enriched", enriched=enriched, artists_checked=checked)
    return {"enriched": enriched, "artists_checked": checked, "cached": len(instrument_cache)}

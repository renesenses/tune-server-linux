from __future__ import annotations

import asyncio
import re
from pathlib import Path

import aiohttp
import structlog

from tune_server.db.engine import Database
from tune_server.db.sa_repository import AlbumRepo, ArtistRepo

logger = structlog.get_logger()

DISCOGS_API = "https://api.discogs.com"
DISCOGS_UA = "TuneServer/0.5.2 +https://mozaiklabs.fr"

# Image source priority (higher index = higher priority)
_IMAGE_SOURCE_PRIORITY = {
    None: 0,
    "wikipedia": 1,
    "musicbrainz": 2,
    "community": 3,
    "discogs": 4,
    "user": 5,
}


def image_source_priority(source: str | None) -> int:
    """Return numeric priority for an image source. Higher = more trusted."""
    return _IMAGE_SOURCE_PRIORITY.get(source, 0)


def should_update_image(current_source: str | None, new_source: str | None) -> bool:
    """Return True if new_source has equal or higher priority than current_source."""
    return image_source_priority(new_source) >= image_source_priority(current_source)


# Normalize MusicBrainz tags to Discogs-style genres
_GENRE_NORMALIZE = {
    "rock": "Rock", "pop": "Pop", "jazz": "Jazz", "blues": "Blues",
    "electronic": "Electronic", "classical": "Classical", "hip hop": "Hip-Hop",
    "hip-hop": "Hip-Hop", "rap": "Hip-Hop", "soul": "Soul / R&B", "r&b": "Soul / R&B",
    "funk": "Funk", "reggae": "Reggae", "world": "World", "latin": "Latin",
    "country": "Country", "folk": "Folk", "metal": "Metal", "punk": "Punk",
    "alternative": "Alternative", "indie": "Indie", "alternative rock": "Alternative Rock",
    "hard rock": "Hard Rock", "progressive rock": "Progressive Rock", "classic rock": "Classic Rock",
    "chanson": "Chanson Française", "chanson française": "Chanson Française",
    "french pop": "Chanson Française", "bossa nova": "Jazz", "bebop": "Jazz",
    "swing": "Jazz", "big band": "Jazz", "smooth jazz": "Jazz",
    "ambient": "Electronic", "house": "Electronic", "techno": "Electronic",
    "downtempo": "Electronic", "trip hop": "Electronic", "electro": "Electronic",
    "opera": "Classical", "baroque": "Classical", "romantic": "Classical",
    "symphony": "Classical", "chamber music": "Classical",
    "soundtrack": "Soundtrack", "score": "Soundtrack",
    "disco": "Pop", "new wave": "Pop",
    # French genre names
    "alternatif": "Rock", "indé": "Rock", "indépendant": "Rock",
    "indie rock": "Rock", "alt": "Rock",
    "classique": "Classical", "musique classique": "Classical",
    "électronique": "Electronic", "electronique": "Electronic",
    "musique électronique": "Electronic",
    "variété": "Pop", "variété française": "Pop",
    "variétés": "Pop", "variétés françaises": "Pop",
    "musique du monde": "World", "musiques du monde": "World",
    "bande originale": "Soundtrack", "musique de film": "Soundtrack",
    "chanson francaise": "Chanson Française",
}

# Separators that tag editors use for multi-value genre fields
_GENRE_SEPARATORS = re.compile(r"\s*[;&/]\s*|,\s+")


def normalize_genre(raw: str) -> str:
    """Normalize a raw genre tag to a Discogs-style genre.

    Handles multi-value separators (& / ; ,) by splitting and returning
    the first recognized genre. Falls back to the original value (title-cased)
    when no match is found — never returns "Other".
    """
    if not raw or not raw.strip():
        return raw

    # Try the whole string first (before splitting)
    lower = raw.lower().strip()
    if lower in _GENRE_NORMALIZE:
        return _GENRE_NORMALIZE[lower]

    # Split on common separators and try each part
    parts = _GENRE_SEPARATORS.split(raw.strip())
    if len(parts) > 1:
        for part in parts:
            part = part.strip()
            if not part:
                continue
            pl = part.lower()
            if pl in _GENRE_NORMALIZE:
                return _GENRE_NORMALIZE[pl]
        # No part matched — use the first non-empty part as-is
        for part in parts:
            part = part.strip()
            if part:
                return part.title()

    # No match, no splitting — keep original value, title-cased
    return raw.strip().title()


async def _fetch_discogs_artist_image(name: str, token: str, cache_dir: str) -> str | None:
    """Search Discogs for an artist and download their image. Returns local cache path or None."""
    headers = {
        "Authorization": f"Discogs token={token}",
        "User-Agent": DISCOGS_UA,
    }
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as session:
            # Search artist
            async with session.get(f"{DISCOGS_API}/database/search", params={"q": name, "type": "artist", "per_page": 1}) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                results = data.get("results", [])
                if not results:
                    return None
                artist_data = results[0]
                cover_url = artist_data.get("cover_image") or artist_data.get("thumb")
                discogs_id = str(artist_data.get("id", ""))
                if not cover_url or "spacer.gif" in cover_url:
                    return None

            # Download image
            async with session.get(cover_url) as img_resp:
                if img_resp.status != 200:
                    return None
                img_data = await img_resp.read()
                if len(img_data) < 1000:
                    return None

            # Save to cache
            import hashlib
            ext = "jpg"
            hash_name = hashlib.md5(cover_url.encode()).hexdigest()
            cache_path = Path(cache_dir) / f"{hash_name}.{ext}"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(img_data)
            logger.info("discogs_artist_image_saved", artist=name, path=str(cache_path), discogs_id=discogs_id)
            return f"{cache_dir}/{hash_name}.{ext}"
    except Exception:
        logger.debug("discogs_artist_image_error", artist=name)
        return None


async def _fetch_community_image(mbid: str, cache_dir: str) -> str | None:
    """Check mozaiklabs.fr community cache for an artist image. Returns local path or None."""
    from tune_server.config import settings as _s
    if not _s.community_cache_enabled or not mbid:
        return None
    url = f"{_s.community_api_url}/{mbid}/image"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                image_url = data.get("image_url")
                if not image_url:
                    return None

            # Download the image
            full_url = f"https://mozaiklabs.fr{image_url}" if image_url.startswith("/") else image_url
            async with session.get(full_url) as img_resp:
                if img_resp.status != 200:
                    return None
                img_data = await img_resp.read()
                if len(img_data) < 1000:
                    return None

            # Save to local cache
            cache_path = Path(cache_dir) / f"community_{mbid}.jpg"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(img_data)
            logger.info("community_artist_image_cached", mbid=mbid, path=str(cache_path))
            return str(cache_path)
    except Exception:
        logger.debug("community_image_fetch_failed", mbid=mbid)
        return None


async def _share_image_to_community(mbid: str, artist_name: str, image_path: str, source: str) -> None:
    """Upload a successfully fetched artist image to the mozaiklabs.fr community cache."""
    from tune_server.config import settings as _s
    if not _s.community_cache_enabled or not _s.community_api_key or not mbid:
        return
    url = f"{_s.community_api_url}/{mbid}/image"
    try:
        file_path = Path(image_path)
        if not file_path.exists():
            return
        headers = {"Authorization": f"Bearer {_s.community_api_key}"}
        data = aiohttp.FormData()
        data.add_field("file", file_path.read_bytes(), filename=f"{mbid}.jpg", content_type="image/jpeg")
        data.add_field("source", source)
        data.add_field("artist_name", artist_name or "")

        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(url, data=data) as resp:
                if resp.status in (200, 201):
                    logger.info("community_image_shared", mbid=mbid, artist=artist_name)
                else:
                    logger.debug("community_image_share_failed", mbid=mbid, status=resp.status)
    except Exception:
        logger.debug("community_image_share_error", mbid=mbid)


async def _report_community_image(mbid: str) -> None:
    """Report an incorrect image to the mozaiklabs.fr community cache."""
    from tune_server.config import settings as _s
    if not _s.community_cache_enabled or not _s.community_api_key or not mbid:
        return
    url = f"{_s.community_api_url}/{mbid}/image/report"
    try:
        headers = {"Authorization": f"Bearer {_s.community_api_key}"}
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(url) as resp:
                if resp.status == 200:
                    logger.info("community_image_reported", mbid=mbid)
                else:
                    logger.debug("community_image_report_failed", mbid=mbid, status=resp.status)
    except Exception:
        logger.debug("community_image_report_error", mbid=mbid)


class MetadataEnricher:
    """Background enrichment from MusicBrainz and Discogs."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._artist_repo = ArtistRepo(db)
        self._album_repo = AlbumRepo(db)
        self._task: asyncio.Task | None = None
        self._running = False

    async def _get_discogs_token(self) -> str:
        """Return Discogs token: .env first, then DB (Services UI) fallback."""
        from tune_server.config import settings as _s
        if _s.discogs_token:
            return _s.discogs_token
        try:
            import json as _json
            row = await self._db.fetchone(
                "SELECT token_data FROM streaming_auth WHERE service = ?",
                ("discogs",),
            )
            if row and row["token_data"]:
                data = _json.loads(row["token_data"])
                token = data.get("token", "")
                if token:
                    return token
        except Exception:
            pass
        return ""

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._enrich_loop())
        logger.info("metadata_enricher_started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.debug("enrichment_task_cancelled")
            self._task = None

    async def enrich_now(self) -> None:
        """Trigger one immediate enrichment pass in the background."""
        asyncio.create_task(self._single_pass())
        logger.info("metadata_enrichment_triggered")

    async def _single_pass(self) -> None:
        """Run one enrichment pass (same logic as the loop body)."""
        try:
            import musicbrainzngs
            musicbrainzngs.set_useragent("TuneServer", "0.1.0", "")
        except ImportError:
            logger.warning("musicbrainzngs_not_installed")
            return

        try:
            # Enrich artists
            artists = await self._artist_repo.list(limit=50)
            for artist in artists:
                if artist.musicbrainz_id:
                    continue
                try:
                    result = await asyncio.to_thread(
                        musicbrainzngs.search_artists, artist=artist.name, limit=1
                    )
                    mb_artists = result.get("artist-list", [])
                    if mb_artists:
                        mb = mb_artists[0]
                        artist.musicbrainz_id = mb.get("id")
                        artist.sort_name = mb.get("sort-name", artist.sort_name)
                        if not artist.bio:
                            artist.bio = mb.get("disambiguation")
                        await self._artist_repo.update(artist)
                        logger.debug("artist_enriched", name=artist.name, mb_id=artist.musicbrainz_id)
                except Exception:
                    logger.debug("musicbrainz_lookup_failed", artist=artist.name)
                await asyncio.sleep(1.2)

            # Enrich artist images: community cache first, then Discogs
            from tune_server.config import settings as _s
            all_artists = await self._artist_repo.list(limit=200)
            # Only target artists without image or with a lower-priority source
            candidates = [a for a in all_artists
                          if (not a.image_path and a.image_source != "user")
                          or (a.image_path and a.image_source not in ("discogs", "user", "community"))]
            enriched_count = 0
            for artist in candidates:
                # Step 1: Try community cache (free, no rate limit issues)
                if artist.musicbrainz_id:
                    community_path = await _fetch_community_image(
                        artist.musicbrainz_id, _s.artwork_cache_dir
                    )
                    if community_path:
                        artist.image_path = community_path
                        artist.image_source = "community"
                        try:
                            await self._artist_repo.update(artist)
                        except Exception:
                            logger.debug("artist_image_update_failed", name=artist.name)
                            continue
                        enriched_count += 1
                        continue

                # Step 2: Fall back to Discogs (rate-limited)
                _discogs_tok = await self._get_discogs_token()
                if _discogs_tok:
                    img_path = await _fetch_discogs_artist_image(
                        artist.name, _discogs_tok, _s.artwork_cache_dir
                    )
                    if img_path:
                        artist.image_path = img_path
                        artist.image_source = "discogs"
                        try:
                            await self._artist_repo.update(artist)
                        except Exception:
                            logger.debug("artist_image_update_failed", name=artist.name)
                            continue
                        enriched_count += 1
                        # Share to community cache (fire-and-forget)
                        if artist.musicbrainz_id:
                            asyncio.create_task(_share_image_to_community(
                                artist.musicbrainz_id, artist.name, img_path, "discogs"
                            ))
                    await asyncio.sleep(1.5)
            if enriched_count:
                logger.info("artist_images_enriched", count=enriched_count)

            # Enrich albums
            all_albums = await self._album_repo.list(limit=50)
            albums_to_enrich = [a for a in all_albums if not a.year or not a.genre]
            for album in albums_to_enrich:
                album_id = album.id
                album_title = album.title
                if not album_title:
                    continue
                artist_name = album.artist_name or ""
                try:
                    query = f'release:"{album_title}"'
                    if artist_name:
                        query = f'artist:"{artist_name}" AND {query}'
                    result = await asyncio.to_thread(
                        musicbrainzngs.search_releases, query=query, limit=1
                    )
                    releases = result.get("release-list", [])
                    if releases:
                        mb = releases[0]
                        updates = {}
                        date_str = mb.get("date", "")
                        if date_str and len(date_str) >= 4:
                            try:
                                year = int(date_str[:4])
                                if 1900 <= year <= 2030:
                                    updates["year"] = year
                            except ValueError as e:
                                logger.debug("enrichment_year_parse_failed", date_str=date_str, error=str(e))
                        tags = mb.get("tag-list", [])
                        if tags:
                            top_tag = sorted(tags, key=lambda t: int(t.get("count", 0)), reverse=True)[0]
                            genre = normalize_genre(top_tag.get("name", ""))
                            if genre:
                                updates["genre"] = genre
                        if updates:
                            album = await self._album_repo.get(album_id)
                            if album:
                                if "year" in updates and not album.year:
                                    album.year = updates["year"]
                                if "genre" in updates and not album.genre:
                                    album.genre = updates["genre"]
                                await self._album_repo.update(album)
                                logger.debug("album_enriched", title=album_title, **updates)
                except Exception:
                    logger.debug("album_enrichment_failed", title=album_title)
                await asyncio.sleep(1.2)
        except Exception:
            logger.exception("single_pass_enrichment_error")

    async def _enrich_loop(self) -> None:
        try:
            import musicbrainzngs
            musicbrainzngs.set_useragent("TuneServer", "0.1.0", "")
        except ImportError:
            logger.warning("musicbrainzngs_not_installed")
            return

        while self._running:
            try:
                # Find artists without MusicBrainz IDs
                artists = await self._artist_repo.list(limit=10)
                for artist in artists:
                    if artist.musicbrainz_id or not self._running:
                        continue

                    try:
                        result = await asyncio.to_thread(
                            musicbrainzngs.search_artists,
                            artist=artist.name,
                            limit=1,
                        )
                        mb_artists = result.get("artist-list", [])
                        if mb_artists:
                            mb = mb_artists[0]
                            artist.musicbrainz_id = mb.get("id")
                            artist.sort_name = mb.get("sort-name", artist.sort_name)
                            if not artist.bio:
                                artist.bio = mb.get("disambiguation")
                            await self._artist_repo.update(artist)
                            logger.debug("artist_enriched", name=artist.name, mb_id=artist.musicbrainz_id)
                    except Exception:
                        logger.debug("musicbrainz_lookup_failed", artist=artist.name)

                    # Rate limit: MusicBrainz allows 1 request per second
                    await asyncio.sleep(1.5)

                # Enrich artist images: community cache first, then Discogs
                from tune_server.config import settings as _s
                all_artists = await self._artist_repo.list(limit=200)
                # Only target artists without image or with a lower-priority source
                no_image = [a for a in all_artists
                            if (not a.image_path and a.image_source != "user")
                            or (a.image_path and a.image_source not in ("discogs", "user", "community"))]
                enriched_count = 0
                for artist in no_image:
                    if not self._running:
                        break

                    # Step 1: Try community cache (free, no rate limit issues)
                    if artist.musicbrainz_id:
                        community_path = await _fetch_community_image(
                            artist.musicbrainz_id, _s.artwork_cache_dir
                        )
                        if community_path:
                            artist.image_path = community_path
                            artist.image_source = "community"
                            try:
                                await self._artist_repo.update(artist)
                            except Exception:
                                logger.debug("artist_image_update_failed", name=artist.name)
                                continue
                            enriched_count += 1
                            continue

                    # Step 2: Fall back to Discogs (rate-limited)
                    _discogs_tok2 = await self._get_discogs_token()
                    if _discogs_tok2:
                        img_path = await _fetch_discogs_artist_image(
                            artist.name, _discogs_tok2, _s.artwork_cache_dir
                        )
                        if img_path:
                            artist.image_path = img_path
                            artist.image_source = "discogs"
                            try:
                                await self._artist_repo.update(artist)
                            except Exception:
                                logger.debug("artist_image_update_failed", name=artist.name)
                                continue
                            enriched_count += 1
                            # Share to community cache (fire-and-forget)
                            if artist.musicbrainz_id:
                                asyncio.create_task(_share_image_to_community(
                                    artist.musicbrainz_id, artist.name, img_path, "discogs"
                                ))
                        await asyncio.sleep(1.5)
                if enriched_count:
                    logger.info("artist_images_enriched", count=enriched_count)

                # Enrich albums without year or genre
                all_albums = await self._album_repo.list(limit=50)
                albums_to_enrich = [a for a in all_albums if not a.year or not a.genre]
                for album in albums_to_enrich:
                    if not self._running:
                        break
                    album_id = album.id
                    album_title = album.title
                    if not album_title:
                        continue
                    artist_name = album.artist_name or ""

                    try:
                        query = f'release:"{album_title}"'
                        if artist_name:
                            query = f'artist:"{artist_name}" AND {query}'
                        result = await asyncio.to_thread(
                            musicbrainzngs.search_releases,
                            query=query,
                            limit=1,
                        )
                        releases = result.get("release-list", [])
                        if releases:
                            mb = releases[0]
                            updates = {}
                            # Extract year from date
                            date_str = mb.get("date", "")
                            if date_str and len(date_str) >= 4:
                                try:
                                    year = int(date_str[:4])
                                    if 1900 <= year <= 2030:
                                        updates["year"] = year
                                except ValueError as e:
                                    logger.debug("enrichment_year_parse_failed", date_str=date_str, error=str(e))
                            # Extract genre from tags
                            tags = mb.get("tag-list", [])
                            if tags:
                                top_tag = sorted(tags, key=lambda t: int(t.get("count", 0)), reverse=True)[0]
                                genre = top_tag.get("name", "").title()
                                if genre:
                                    updates["genre"] = genre
                            if updates:
                                album = await self._album_repo.get(album_id)
                                if album:
                                    if "year" in updates and not album.year:
                                        album.year = updates["year"]
                                    if "genre" in updates and not album.genre:
                                        album.genre = updates["genre"]
                                    await self._album_repo.update(album)
                                    logger.debug("album_enriched", title=album_title, **updates)
                    except Exception:
                        logger.debug("album_enrichment_failed", title=album_title)

                    await asyncio.sleep(1.5)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("enrichment_loop_error")

            # Wait before next enrichment pass
            await asyncio.sleep(60)

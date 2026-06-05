"""Repositories using SQLAlchemy Core expressions — database independent.

Drop-in replacement for repository.py. Each repo uses SA Table objects
instead of raw SQL strings. No engine-specific branches.
"""
from __future__ import annotations

from typing import Optional

import sqlalchemy as sa
import structlog

from tune_server.db.sa_engine import SADatabase
from tune_server.db.tables import (
    artists, albums, tracks, playlists, playlist_tracks,
    zones, play_queue, streaming_auth, radio_favorites, radio_stations,
    user_profiles, user_favorites, party_votes, album_ratings,
    track_credits, playback_history, smart_playlists,
)
from tune_server.models import Album, Artist, Playlist, RadioStation, RadioStationCreate, SearchResult, Track, TrackCredit
from tune_server.utils import fold_accents, sanitize_fts_query

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Row → Model converters (shared with legacy repos)
# ---------------------------------------------------------------------------

def _row_to_artist(row) -> Artist:
    return Artist(
        id=row["id"],
        name=row["name"],
        sort_name=row.get("sort_name") if hasattr(row, "get") else row["sort_name"],
        musicbrainz_id=row.get("musicbrainz_id") if hasattr(row, "get") else row["musicbrainz_id"],
        discogs_id=row.get("discogs_id") if hasattr(row, "get") else row["discogs_id"],
        bio=row.get("bio") if hasattr(row, "get") else row["bio"],
        image_path=row.get("image_path") if hasattr(row, "get") else row["image_path"],
        image_source=row.get("image_source") if hasattr(row, "get") else row["image_source"],
    )


def _quality_from_audio(sample_rate: int | None, bit_depth: int | None, fmt: str | None) -> str:
    if fmt and fmt in ("dsd", "dsf", "dff"):
        return "dsd"
    if sample_rate and sample_rate >= 2_000_000:
        return "dsd"
    if sample_rate and sample_rate > 44100:
        return "hi-res"
    if bit_depth and bit_depth > 16:
        return "hi-res"
    if fmt and fmt in ("mp3", "aac", "ogg", "opus", "wma"):
        return "lossy"
    return "cd"


def _row_to_album(row) -> Album:
    keys = row.keys() if hasattr(row, "keys") else []
    sr = row.get("max_sample_rate") if "max_sample_rate" in keys else None
    bd = row.get("max_bit_depth") if "max_bit_depth" in keys else None
    fmt = row.get("dominant_format") if "dominant_format" in keys else None
    artist_name = (row.get("artist_name_resolved") if "artist_name_resolved" in keys
                   else row.get("artist_name") if "artist_name" in keys else None)
    return Album(
        id=row["id"],
        title=row["title"],
        artist_id=row["artist_id"],
        artist_name=artist_name,
        year=row["year"],
        genre=row["genre"],
        disc_count=row["disc_count"],
        track_count=row["track_count"],
        cover_path=row["cover_path"],
        source=row["source"],
        source_id=row["source_id"],
        sample_rate=sr,
        bit_depth=bd,
        format=fmt,
        quality=_quality_from_audio(sr, bd, fmt) if sr or bd or fmt else None,
        bio=row.get("bio") if "bio" in keys else None,
        label=row.get("label") if "label" in keys else None,
        catalog_number=row.get("catalog_number") if "catalog_number" in keys else None,
        musicbrainz_release_id=row.get("musicbrainz_release_id") if "musicbrainz_release_id" in keys else None,
        musicbrainz_release_group_id=row.get("musicbrainz_release_group_id") if "musicbrainz_release_group_id" in keys else None,
        original_year=row.get("original_year") if "original_year" in keys else None,
        release_date=row.get("release_date") if "release_date" in keys else None,
        original_date=row.get("original_date") if "original_date" in keys else None,
    )


def _row_to_track(row) -> Track:
    keys = row.keys() if hasattr(row, "keys") else []
    # Prefer resolved (joined) values, fallback to denormalized columns
    album_title = (row.get("album_title_resolved") if "album_title_resolved" in keys
                   else row.get("album_title") if "album_title" in keys else None)
    artist_name = (row.get("artist_name_resolved") if "artist_name_resolved" in keys
                   else row.get("artist_name") if "artist_name" in keys else None)
    cover_path = (row.get("cover_path_resolved") if "cover_path_resolved" in keys
                  else row.get("cover_path") if "cover_path" in keys else None)
    return Track(
        id=row["id"],
        title=row["title"],
        album_id=row["album_id"],
        album_title=album_title,
        artist_id=row["artist_id"],
        artist_name=artist_name,
        disc_number=row["disc_number"],
        disc_subtitle=row.get("disc_subtitle") if "disc_subtitle" in keys else None,
        track_number=row["track_number"],
        duration_ms=row["duration_ms"],
        file_path=row["file_path"],
        format=row["format"],
        sample_rate=row["sample_rate"],
        bit_depth=row["bit_depth"],
        channels=row["channels"],
        cover_path=cover_path,
        source=row["source"],
        source_id=row["source_id"],
        isrc=row.get("isrc") if "isrc" in keys else None,
        bpm=row.get("bpm") if "bpm" in keys else None,
        waveform_data=row.get("waveform_data") if "waveform_data" in keys else None,
        waveform_generated_at=str(row["waveform_generated_at"]) if "waveform_generated_at" in keys and row.get("waveform_generated_at") else None,
        musicbrainz_recording_id=row.get("musicbrainz_recording_id") if "musicbrainz_recording_id" in keys else None,
    )


def _row_to_playlist(row) -> Playlist:
    keys = row.keys() if hasattr(row, "keys") else []
    return Playlist(
        id=row["id"],
        name=row["name"],
        description=row.get("description"),
        track_count=row.get("track_count", 0) if "track_count" in keys else 0,
    )


def _row_to_radio_station(row) -> RadioStation:
    return RadioStation(
        id=row["id"],
        name=row["name"],
        stream_url=row["stream_url"],
        logo_url=row.get("logo_url"),
        genre=row.get("genre"),
        tags=row.get("tags"),
        codec=row.get("codec"),
        country=row.get("country"),
        homepage_url=row.get("homepage_url"),
        favorite=bool(row.get("favorite", False)),
    )


# ===================================================================
# ArtistRepo — SA Core
# ===================================================================

class SAArtistRepo:
    def __init__(self, db: SADatabase) -> None:
        self._db = db

    async def get(self, artist_id: int) -> Optional[Artist]:
        row = await self._db.sa_fetchone(
            sa.select(artists).where(artists.c.id == artist_id)
        )
        return _row_to_artist(row) if row else None

    async def get_by_name(self, name: str) -> Optional[Artist]:
        row = await self._db.sa_fetchone(
            sa.select(artists).where(artists.c.name == name)
        )
        return _row_to_artist(row) if row else None

    @staticmethod
    def _principal_only_clause():
        """Filter: artist must own ≥1 album or be the primary artist on ≥1 track.

        Excludes 'credit-only' artists — composers/performers/conductors
        populated from track_credits without their own albums or tracks.
        Without this, the Artists grid is polluted by hundreds of
        collaborators (e.g. a Bach cantata's track credits create dozens
        of vocalist entries that have no other content in the library).
        """
        return sa.or_(
            sa.select(albums.c.id).where(albums.c.artist_id == artists.c.id).exists(),
            sa.select(tracks.c.id).where(tracks.c.artist_id == artists.c.id).exists(),
        )

    async def list(self, limit: int = 100, offset: int = 0, principal_only: bool = False) -> list[Artist]:
        stmt = sa.select(artists)
        if principal_only:
            stmt = stmt.where(self._principal_only_clause())
        rows = await self._db.sa_fetchall(
            stmt.order_by(sa.func.lower(sa.func.coalesce(artists.c.sort_name, artists.c.name)), artists.c.name)
                .limit(limit).offset(offset)
        )
        return [_row_to_artist(r) for r in rows]

    async def count(self, principal_only: bool = False) -> int:
        stmt = sa.select(sa.func.count()).select_from(artists)
        if principal_only:
            stmt = stmt.where(self._principal_only_clause())
        row = await self._db.sa_fetchone(stmt)
        return row[0] if row else 0

    async def list_initial_letters(self, principal_only: bool = False) -> list[tuple[str, int]]:
        letter = sa.case(
            (sa.func.upper(sa.func.substr(sa.func.coalesce(artists.c.sort_name, artists.c.name), 1, 1))
             .between("A", "Z"),
             sa.func.upper(sa.func.substr(sa.func.coalesce(artists.c.sort_name, artists.c.name), 1, 1))),
            else_="#",
        ).label("letter")
        stmt = sa.select(letter, sa.func.count().label("cnt"))
        if principal_only:
            stmt = stmt.where(self._principal_only_clause())
        stmt = stmt.group_by(letter).order_by(letter)
        rows = await self._db.sa_fetchall(stmt)
        return [(r["letter"], r["cnt"]) for r in rows]

    async def list_by_letter(self, letter: str, limit: int = 500, offset: int = 0, principal_only: bool = False) -> list[Artist]:
        first_char = sa.func.upper(sa.func.substr(
            sa.func.coalesce(artists.c.sort_name, artists.c.name), 1, 1
        ))
        if letter == "#":
            where = ~first_char.between("A", "Z")
        else:
            where = first_char == letter.upper()
        if principal_only:
            where = sa.and_(where, self._principal_only_clause())
        rows = await self._db.sa_fetchall(
            sa.select(artists).where(where)
            .order_by(sa.func.lower(sa.func.coalesce(artists.c.sort_name, artists.c.name)), artists.c.name)
            .limit(limit).offset(offset)
        )
        return [_row_to_artist(r) for r in rows]

    async def create(self, artist: Artist) -> int:
        result = await self._db.sa_execute(
            artists.insert().values(
                name=artist.name,
                sort_name=artist.sort_name,
                musicbrainz_id=artist.musicbrainz_id,
                discogs_id=artist.discogs_id,
                bio=artist.bio,
                image_path=artist.image_path,
                image_source=artist.image_source,
            )
        )
        return result.lastrowid

    async def get_by_musicbrainz_id(self, mbid: str) -> Artist | None:
        row = await self._db.sa_fetchone(
            sa.select(artists).where(artists.c.musicbrainz_id == mbid)
        )
        return _row_to_artist(row) if row else None

    @staticmethod
    def _normalize_sort_name(s: str) -> str:
        import unicodedata
        nfkd = unicodedata.normalize("NFKD", s)
        return "".join(c for c in nfkd if unicodedata.category(c) != "Mn")

    async def get_or_create(self, name: str, musicbrainz_id: str | None = None,
                            sort_name: str | None = None) -> Artist:
        if sort_name:
            sort_name = self._normalize_sort_name(sort_name)
        if musicbrainz_id:
            existing = await self.get_by_musicbrainz_id(musicbrainz_id)
            if existing:
                if sort_name and existing.sort_name != sort_name:
                    existing.sort_name = sort_name
                    await self.update(existing)
                return existing
        existing = await self.get_by_name(name)
        if existing:
            if musicbrainz_id and not existing.musicbrainz_id:
                existing.musicbrainz_id = musicbrainz_id
                await self.update(existing)
            if sort_name and existing.sort_name != sort_name:
                existing.sort_name = sort_name
                await self.update(existing)
            return existing
        effective_sort = sort_name or self._normalize_sort_name(name)
        artist = Artist(name=name, sort_name=effective_sort, musicbrainz_id=musicbrainz_id)
        artist_id = await self.create(artist)
        return Artist(id=artist_id, name=name, sort_name=effective_sort, musicbrainz_id=musicbrainz_id)

    async def update(self, artist: Artist) -> None:
        await self._db.sa_execute(
            artists.update().where(artists.c.id == artist.id).values(
                name=artist.name,
                sort_name=artist.sort_name,
                musicbrainz_id=artist.musicbrainz_id,
                discogs_id=artist.discogs_id,
                bio=artist.bio,
                image_path=artist.image_path,
                image_source=artist.image_source,
                updated_at=sa.func.now(),
            )
        )

    async def delete(self, artist_id: int) -> None:
        await self._db.sa_execute(
            artists.delete().where(artists.c.id == artist_id)
        )

    async def count_without_image(self) -> int:
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(artists).where(
                sa.or_(artists.c.image_path.is_(None), artists.c.image_path == "")
            )
        )
        return row[0] if row else 0

    async def search(self, query: str, limit: int = 50) -> list[Artist]:
        """FTS search with accent-folded LIKE fallback."""
        where_clause = self._db.fts.search_where("artists", query)
        rank_clause = self._db.fts.search_rank("artists", query)

        folded = fold_accents(query)
        like_folded = f"%{folded}%"

        # Build query with FTS plugin clauses + accent-folded LIKE fallback
        fts_query = sanitize_fts_query(query) + "*" if self._db.engine_name == "sqlite" else query
        like_col = "name"
        if self._db.engine_name in ("postgres", "postgresql"):
            accent_fallback = sa.text(f"unaccent(artists.{like_col}) ILIKE :like_folded")
        else:
            accent_fallback = sa.text(f"fold_accents(artists.{like_col}) LIKE :like_folded")
        stmt = (
            sa.select(artists)
            .where(sa.or_(where_clause, accent_fallback))
            .order_by(sa.desc(rank_clause))
            .limit(limit)
            .params(fts_query=fts_query, like_folded=like_folded)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_artist(r) for r in rows]


# ===================================================================
# AlbumRepo — SA Core
# ===================================================================

class SAAlbumRepo:
    def __init__(self, db: SADatabase) -> None:
        self._db = db

    def _album_select(self):
        """Base SELECT with artist name and denormalized quality columns."""
        return (
            sa.select(
                albums,
                sa.func.coalesce(artists.c.name, albums.c.artist_name).label("artist_name_resolved"),
                albums.c.sample_rate.label("max_sample_rate"),
                albums.c.bit_depth.label("max_bit_depth"),
                albums.c.format.label("dominant_format"),
            )
            .outerjoin(artists, albums.c.artist_id == artists.c.id)
        )

    async def get(self, album_id: int) -> Optional[Album]:
        stmt = self._album_select().where(albums.c.id == album_id)
        row = await self._db.sa_fetchone(stmt)
        return _row_to_album(row) if row else None

    async def list(self, limit: int = 100, offset: int = 0,
                   quality: str | None = None, format: str | None = None,
                   sample_rate: int | None = None,
                   sort: str = "title", order: str = "asc") -> list[Album]:
        stmt = self._album_select()

        # Quality/format/sample_rate filters via track subquery
        if quality or format or sample_rate:
            has_track = sa.exists(
                sa.select(sa.literal(1)).select_from(tracks)
                .where(tracks.c.album_id == albums.c.id)
            )
            if format:
                has_track = sa.exists(
                    sa.select(sa.literal(1)).select_from(tracks)
                    .where(tracks.c.album_id == albums.c.id)
                    .where(tracks.c.format == format)
                )
                stmt = stmt.where(has_track)
            if sample_rate:
                has_track_sr = sa.exists(
                    sa.select(sa.literal(1)).select_from(tracks)
                    .where(tracks.c.album_id == albums.c.id)
                    .where(tracks.c.sample_rate >= sample_rate)
                )
                stmt = stmt.where(has_track_sr)
            if quality == "hi-res":
                has_hires = sa.exists(
                    sa.select(sa.literal(1)).select_from(tracks)
                    .where(tracks.c.album_id == albums.c.id)
                    .where(sa.or_(tracks.c.sample_rate > 44100, tracks.c.bit_depth > 16))
                )
                stmt = stmt.where(has_hires)
            elif quality == "dsd":
                has_dsd = sa.exists(
                    sa.select(sa.literal(1)).select_from(tracks)
                    .where(tracks.c.album_id == albums.c.id)
                    .where(tracks.c.format.in_(["dsd", "dsf", "dff"]))
                )
                stmt = stmt.where(has_dsd)
            elif quality == "lossy":
                has_lossy = sa.exists(
                    sa.select(sa.literal(1)).select_from(tracks)
                    .where(tracks.c.album_id == albums.c.id)
                    .where(tracks.c.format.in_(["mp3", "aac", "ogg", "opus", "wma"]))
                )
                stmt = stmt.where(has_lossy)
            elif quality == "cd":
                has_cd = sa.exists(
                    sa.select(sa.literal(1)).select_from(tracks)
                    .where(tracks.c.album_id == albums.c.id)
                    .where(tracks.c.sample_rate <= 44100)
                    .where(~tracks.c.format.in_(["mp3", "aac", "ogg", "opus", "wma", "dsd", "dsf", "dff"]))
                )
                stmt = stmt.where(has_cd)

        _sort_cols = {
            "title": albums.c.title,
            "artist": sa.func.coalesce(artists.c.name, albums.c.artist_name),
            "release_date": albums.c.year,
            "original_year": albums.c.original_year,
            "added_date": albums.c.created_at,
        }
        sort_col = _sort_cols.get(sort, albums.c.title)
        if order.lower() == "desc":
            sort_expr = sa.nullslast(sort_col.desc())
        else:
            sort_expr = sa.nullslast(sort_col.asc())
        stmt = stmt.order_by(sort_expr).limit(limit).offset(offset)
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_album(r) for r in rows]

    async def count(self) -> int:
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(albums)
        )
        return row[0] if row else 0

    async def list_by_artist(self, artist_id: int) -> list[Album]:
        stmt = (
            self._album_select()
            .where(albums.c.artist_id == artist_id)
            .order_by(sa.case((sa.func.coalesce(albums.c.original_year, albums.c.year).is_(None), 1), else_=0), sa.func.coalesce(albums.c.original_year, albums.c.year), albums.c.title)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_album(r) for r in rows]

    async def list_by_genre(self, genre: str) -> list[Album]:
        stmt = (
            self._album_select()
            .where(albums.c.genre == genre)
            .order_by(albums.c.title)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_album(r) for r in rows]

    async def list_recent(self, limit: int = 20) -> list[Album]:
        stmt = (
            self._album_select()
            .order_by(albums.c.created_at.desc())
            .limit(limit)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_album(r) for r in rows]

    async def create(self, album: Album) -> int:
        result = await self._db.sa_execute(
            albums.insert().values(
                title=album.title,
                artist_id=album.artist_id,
                artist_name=album.artist_name,
                year=album.year,
                original_year=album.original_year,
                release_date=album.release_date,
                original_date=album.original_date,
                genre=album.genre,
                cover_path=album.cover_path,
                source=album.source or "local",
                source_id=album.source_id,
                label=album.label,
                catalog_number=album.catalog_number,
                musicbrainz_release_id=album.musicbrainz_release_id,
                musicbrainz_release_group_id=album.musicbrainz_release_group_id,
            )
        )
        return result.lastrowid

    async def update(self, album: Album) -> None:
        await self._db.sa_execute(
            albums.update().where(albums.c.id == album.id).values(
                title=album.title,
                artist_id=album.artist_id,
                artist_name=album.artist_name,
                year=album.year,
                genre=album.genre,
                cover_path=album.cover_path,
                label=album.label,
                catalog_number=album.catalog_number,
                updated_at=sa.func.now(),
            )
        )

    async def update_label(self, album_id: int, label: str | None,
                            catalog_number: str | None = None) -> None:
        """Backfill label / catalog_number if the album doesn't have them yet."""
        existing = await self._db.sa_fetchone(
            sa.select(albums.c.label, albums.c.catalog_number).where(albums.c.id == album_id)
        )
        if not existing:
            return
        new_label = existing.get("label") or label
        new_cat = existing.get("catalog_number") or catalog_number
        if new_label != existing.get("label") or new_cat != existing.get("catalog_number"):
            await self._db.sa_execute(
                albums.update().where(albums.c.id == album_id).values(
                    label=new_label,
                    catalog_number=new_cat,
                    updated_at=sa.func.now(),
                )
            )

    async def get_or_create(self, title: str, artist_id: int, **kwargs) -> Album:
        """Compat with legacy AlbumRepo.get_or_create."""
        mb_release_id = kwargs.get("musicbrainz_release_id")
        if mb_release_id:
            row = await self._db.sa_fetchone(
                self._album_select().where(albums.c.musicbrainz_release_id == mb_release_id)
            )
            if row:
                return _row_to_album(row)
        year = kwargs.get("year")
        stmt = self._album_select().where(
            sa.and_(albums.c.title == title, albums.c.artist_id == artist_id)
        )
        if year:
            row = await self._db.sa_fetchone(stmt.where(albums.c.year == year))
            if row:
                existing = _row_to_album(row)
                if mb_release_id and existing.musicbrainz_release_id and existing.musicbrainz_release_id != mb_release_id:
                    pass  # Don't reuse
                else:
                    if mb_release_id and not existing.musicbrainz_release_id:
                        await self.update_musicbrainz_ids(existing.id, mb_release_id, kwargs.get("musicbrainz_release_group_id"))
                    return existing
        row = await self._db.sa_fetchone(stmt)
        if row:
            existing = _row_to_album(row)
            if mb_release_id and existing.musicbrainz_release_id and existing.musicbrainz_release_id != mb_release_id:
                pass  # Don't reuse
            elif year and existing.year and existing.year != year:
                pass  # Don't reuse
            else:
                if mb_release_id and not existing.musicbrainz_release_id:
                    await self.update_musicbrainz_ids(existing.id, mb_release_id, kwargs.get("musicbrainz_release_group_id"))
                return existing
        album = Album(title=title, artist_id=artist_id, **kwargs)
        album_id = await self.create(album)
        album.id = album_id
        return album

    async def update_track_count(self, album_id: int) -> None:
        """Recompute track_count from the tracks table."""
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(tracks).where(tracks.c.album_id == album_id)
        )
        cnt = row[0] if row else 0
        await self._db.sa_execute(
            albums.update().where(albums.c.id == album_id).values(track_count=cnt)
        )

    async def get_by_title(self, title: str, year: int | None = None) -> Album | None:
        if year:
            row = await self._db.sa_fetchone(
                self._album_select().where(
                    sa.and_(albums.c.title == title, albums.c.year == year)
                ).limit(1)
            )
            if row:
                return _row_to_album(row)
        row = await self._db.sa_fetchone(
            self._album_select().where(albums.c.title == title).limit(1)
        )
        return _row_to_album(row) if row else None

    async def get_by_title_and_artist(self, title: str, artist_id: int,
                                       year: int | None = None) -> Optional[Album]:
        if year:
            row = await self._db.sa_fetchone(
                self._album_select().where(
                    sa.and_(albums.c.title == title, albums.c.artist_id == artist_id,
                            albums.c.year == year)
                )
            )
            if row:
                return _row_to_album(row)
        row = await self._db.sa_fetchone(
            self._album_select().where(
                sa.and_(albums.c.title == title, albums.c.artist_id == artist_id)
            )
        )
        return _row_to_album(row) if row else None

    async def get_by_musicbrainz_release_id(self, release_id: str) -> Album | None:
        row = await self._db.sa_fetchone(
            self._album_select().where(albums.c.musicbrainz_release_id == release_id)
        )
        return _row_to_album(row) if row else None

    async def get_dominant_sample_rate(self, album_id: int) -> int | None:
        """Return the most common sample_rate among an album's tracks."""
        row = await self._db.sa_fetchone(
            sa.select(tracks.c.sample_rate)
            .where(sa.and_(tracks.c.album_id == album_id,
                           tracks.c.sample_rate.isnot(None)))
            .group_by(tracks.c.sample_rate)
            .order_by(sa.func.count().desc())
            .limit(1)
        )
        return row[0] if row else None

    async def refresh_quality(self, album_id: int) -> None:
        """Recompute and store format/sample_rate/bit_depth from tracks."""
        sr_row = await self._db.sa_fetchone(
            sa.select(sa.func.max(tracks.c.sample_rate), sa.func.max(tracks.c.bit_depth))
            .where(tracks.c.album_id == album_id)
        )
        fmt_row = await self._db.sa_fetchone(
            sa.select(tracks.c.format)
            .where(sa.and_(tracks.c.album_id == album_id, tracks.c.format.isnot(None)))
            .group_by(tracks.c.format)
            .order_by(sa.func.count().desc())
            .limit(1)
        )
        sr = sr_row[0] if sr_row else None
        bd = sr_row[1] if sr_row else None
        fmt = fmt_row[0] if fmt_row else None
        await self._db.sa_execute(
            albums.update().where(albums.c.id == album_id).values(
                sample_rate=sr, bit_depth=bd, format=fmt,
            )
        )

    async def delete_orphans(self) -> int:
        """Delete albums that have no tracks."""
        # Subquery: album IDs that have at least one track
        has_tracks = sa.select(tracks.c.album_id).where(tracks.c.album_id.isnot(None)).distinct()
        result = await self._db.sa_execute(
            albums.delete().where(~albums.c.id.in_(has_tracks))
        )
        return result.rowcount

    async def delete(self, album_id: int) -> None:
        await self._db.sa_execute(
            albums.delete().where(albums.c.id == album_id)
        )

    async def merge_duplicates(self) -> dict:
        """Merge albums that look like duplicates after light normalization.

        Two albums are considered duplicates when they share:
          - the same case-insensitive trimmed title,
          - the same case-insensitive trimmed artist name,
          - the same quality tier (we group sample_rate into CD-44.1/48,
            hi-res 88+, DSD 2.8M+ — never merge across tiers because the
            scanner intentionally splits albums by quality).

        Within each duplicate group:
          - The album with the most tracks wins (most likely the canonical entry).
          - All tracks of the losing albums are reassigned to the winner.
          - When a track exists in both winner and loser pointing to the SAME
            file_path, the duplicate track row is deleted (keeps the winner's).
          - The losing album rows are then deleted.
          - The winner's track_count is recomputed.

        Returns a dict with merge stats and per-group details.
        """
        from collections import defaultdict

        def quality_tier(sr: int | None) -> str:
            sr = sr or 0
            if sr >= 1000000:
                return "dsd"
            if sr >= 80000:
                return "hires"
            return "cd"

        rows = await self._db.sa_fetchall(
            sa.text(
                """SELECT al.id, al.title, al.artist_id, al.sample_rate,
                          al.format,
                          ar.name AS artist_name,
                          (SELECT COUNT(*) FROM tracks t WHERE t.album_id = al.id) AS track_count
                     FROM albums al
                     LEFT JOIN artists ar ON ar.id = al.artist_id"""
            )
        )

        # Group by (lower title, lower artist, quality tier).
        groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for r in rows:
            title = (r.get("title") or "").strip().lower()
            artist = (r.get("artist_name") or "").strip().lower()
            if not title or not artist:
                continue
            key = (title, artist, quality_tier(r.get("sample_rate")))
            groups[key].append({
                "id": r["id"],
                "title": r.get("title"),
                "artist": r.get("artist_name"),
                "tracks": r.get("track_count") or 0,
                "format": r.get("format"),
            })

        merged = 0
        groups_processed: list[dict] = []
        deleted_track_dupes = 0

        for key, members in groups.items():
            if len(members) < 2:
                continue
            # Pick the winner: most tracks first, then lowest id (stable).
            members.sort(key=lambda m: (-m["tracks"], m["id"]))
            winner = members[0]
            losers = members[1:]

            loser_ids = [m["id"] for m in losers]

            # 1. Re-target every reference to the about-to-be-deleted
            #    duplicate-track rows (loser-side rows whose file_path
            #    matches a track already on the winner) onto the winner's
            #    canonical track id. Without this, the FK
            #    playlist_tracks.track_id ... ON DELETE CASCADE wipes
            #    those entries when we DELETE the duplicate tracks below
            #    — that's how Bertrand lost most of the "Fip select"
            #    playlist after the v0.7.58 merge run.
            await self._db.sa_execute(
                sa.text(
                    """UPDATE playlist_tracks pt
                          SET track_id = winner.id
                         FROM tracks loser
                         JOIN tracks winner
                           ON winner.album_id = :winner_id
                          AND winner.file_path = loser.file_path
                          AND winner.file_path IS NOT NULL
                        WHERE pt.track_id = loser.id
                          AND loser.album_id = ANY(:loser_ids)"""
                ).bindparams(loser_ids=loser_ids, winner_id=winner["id"])
            )
            # Same protection for play_queue (now-playing references the
            # tracks table too via FK CASCADE).
            await self._db.sa_execute(
                sa.text(
                    """UPDATE play_queue q
                          SET track_id = winner.id
                         FROM tracks loser
                         JOIN tracks winner
                           ON winner.album_id = :winner_id
                          AND winner.file_path = loser.file_path
                          AND winner.file_path IS NOT NULL
                        WHERE q.track_id = loser.id
                          AND loser.album_id = ANY(:loser_ids)"""
                ).bindparams(loser_ids=loser_ids, winner_id=winner["id"])
            )

            # 2. Delete tracks in losers that share the same file_path as a
            #    track already in the winner (now safe — references moved).
            await self._db.sa_execute(
                sa.text(
                    """DELETE FROM tracks
                        WHERE album_id = ANY(:loser_ids)
                          AND file_path IS NOT NULL
                          AND file_path IN (
                              SELECT file_path FROM tracks
                               WHERE album_id = :winner_id
                                 AND file_path IS NOT NULL
                          )"""
                ).bindparams(loser_ids=loser_ids, winner_id=winner["id"])
            )

            # 3. Reassign remaining loser tracks (those not duplicated by
            #    file_path on the winner) to the winner.
            for loser_id in loser_ids:
                await self._db.sa_execute(
                    sa.text(
                        "UPDATE tracks SET album_id = :winner WHERE album_id = :loser"
                    ).bindparams(winner=winner["id"], loser=loser_id)
                )

            # 3. Delete loser albums.
            await self._db.sa_execute(
                albums.delete().where(albums.c.id.in_(loser_ids))
            )
            merged += len(losers)

            # 4. Recompute the winner's track_count.
            await self._db.sa_execute(
                sa.text(
                    """UPDATE albums SET track_count = (
                            SELECT COUNT(*) FROM tracks WHERE album_id = :wid
                       ) WHERE id = :wid"""
                ).bindparams(wid=winner["id"])
            )

            groups_processed.append({
                "title": winner["title"],
                "artist": winner["artist"],
                "tier": key[2],
                "winner_id": winner["id"],
                "winner_format": winner["format"],
                "loser_ids": [m["id"] for m in losers],
                "tracks_after": sum(m["tracks"] for m in members),
            })

        return {
            "ok": True,
            "groups_merged": len(groups_processed),
            "albums_deleted": merged,
            "details": groups_processed,
        }

    async def search(self, query: str, limit: int = 50) -> list[Album]:
        where_clause = self._db.fts.search_where("albums", query)
        folded = fold_accents(query)
        like_folded = f"%{folded}%"
        fts_query = sanitize_fts_query(query) + "*" if self._db.engine_name == "sqlite" else query
        if self._db.engine_name in ("postgres", "postgresql"):
            accent_fallback = sa.text("unaccent(albums.title) ILIKE :like_folded")
        else:
            accent_fallback = sa.text("fold_accents(albums.title) LIKE :like_folded")
        stmt = (
            self._album_select()
            .where(sa.or_(where_clause, accent_fallback))
            .limit(limit)
            .params(fts_query=fts_query, like_folded=like_folded)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_album(r) for r in rows]

    async def update_bio(self, album_id: int, bio: str) -> None:
        await self._db.sa_execute(
            albums.update().where(albums.c.id == album_id).values(bio=bio)
        )

    async def list_genres(self) -> list[dict]:
        stmt = (
            sa.select(albums.c.genre, sa.func.count().label("count"))
            .where(albums.c.genre.isnot(None), albums.c.genre != "")
            .group_by(albums.c.genre)
            .order_by(albums.c.genre)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [{"genre": r["genre"], "count": r["count"]} for r in rows]

    async def count_without_cover(self) -> int:
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(albums).where(
                sa.or_(albums.c.cover_path.is_(None), albums.c.cover_path == "")
            )
        )
        return row[0] if row else 0

    async def count_without_genre(self) -> int:
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(albums).where(
                sa.or_(albums.c.genre.is_(None), albums.c.genre == "")
            )
        )
        return row[0] if row else 0

    async def count_without_year(self) -> int:
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(albums).where(
                albums.c.year.is_(None)
            )
        )
        return row[0] if row else 0

    async def list_without_cover(self) -> list[Album]:
        stmt = (
            self._album_select()
            .where(sa.or_(albums.c.cover_path.is_(None), albums.c.cover_path == ""))
            .order_by(albums.c.title)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_album(r) for r in rows]

    async def list_without_musicbrainz_ids(self) -> list[Album]:
        """Return local albums that have no musicbrainz_release_id."""
        stmt = (
            self._album_select()
            .where(
                sa.or_(
                    albums.c.musicbrainz_release_id.is_(None),
                    albums.c.musicbrainz_release_id == "",
                )
            )
            .where(albums.c.source == "local")
            .order_by(albums.c.title)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_album(r) for r in rows]

    async def list_initial_letters(self) -> list[tuple[str, int]]:
        """Return (letter, count) pairs for album titles, A-Z + # for non-alpha."""
        letter = sa.case(
            (sa.func.upper(sa.func.substr(albums.c.title, 1, 1)).between("A", "Z"),
             sa.func.upper(sa.func.substr(albums.c.title, 1, 1))),
            else_="#",
        ).label("letter")
        stmt = (
            sa.select(letter, sa.func.count().label("cnt"))
            .group_by(letter)
            .order_by(letter)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [(r["letter"], r["cnt"]) for r in rows]

    async def count_by_letter(self, letter: str) -> int:
        first_char = sa.func.upper(sa.func.substr(albums.c.title, 1, 1))
        if letter == "#":
            where = ~first_char.between("A", "Z")
        else:
            where = first_char == letter.upper()
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(albums).where(where)
        )
        return row[0] if row else 0

    async def list_by_letter(self, letter: str, limit: int = 500, offset: int = 0) -> list[Album]:
        first_char = sa.func.upper(sa.func.substr(albums.c.title, 1, 1))
        if letter == "#":
            where = ~first_char.between("A", "Z")
        else:
            where = first_char == letter.upper()
        rows = await self._db.sa_fetchall(
            self._album_select().where(where)
            .order_by(albums.c.title)
            .limit(limit).offset(offset)
        )
        return [_row_to_album(r) for r in rows]

    async def update_musicbrainz_ids(self, album_id: int,
                                       release_id: str | None = None,
                                       release_group_id: str | None = None) -> None:
        if release_id:
            await self._db.sa_execute(
                albums.update()
                .where(albums.c.id == album_id)
                .where(
                    sa.or_(
                        albums.c.musicbrainz_release_id.is_(None),
                        albums.c.musicbrainz_release_id == "",
                    )
                )
                .values(musicbrainz_release_id=release_id)
            )
        if release_group_id:
            await self._db.sa_execute(
                albums.update()
                .where(albums.c.id == album_id)
                .where(
                    sa.or_(
                        albums.c.musicbrainz_release_group_id.is_(None),
                        albums.c.musicbrainz_release_group_id == "",
                    )
                )
                .values(musicbrainz_release_group_id=release_group_id)
            )


# ===================================================================
# TrackRepo — SA Core
# ===================================================================

class SATrackRepo:
    def __init__(self, db: SADatabase) -> None:
        self._db = db

    def _track_select(self):
        """Base SELECT with album title and artist name from JOINs.

        Uses COALESCE to prefer the joined value over the denormalized one.
        Labels use _joined suffix to avoid ambiguity with tracks columns.
        """
        return (
            sa.select(
                tracks,
                sa.func.coalesce(albums.c.title, tracks.c.album_title).label("album_title_resolved"),
                sa.func.coalesce(artists.c.name, tracks.c.artist_name).label("artist_name_resolved"),
                sa.func.coalesce(albums.c.cover_path, tracks.c.cover_path).label("cover_path_resolved"),
            )
            .outerjoin(albums, tracks.c.album_id == albums.c.id)
            .outerjoin(artists, tracks.c.artist_id == artists.c.id)
        )

    async def get(self, track_id: int) -> Optional[Track]:
        stmt = self._track_select().where(tracks.c.id == track_id)
        row = await self._db.sa_fetchone(stmt)
        return _row_to_track(row) if row else None

    async def list(self, limit: int = 100, offset: int = 0) -> list[Track]:
        stmt = (
            self._track_select()
            .order_by(tracks.c.title)
            .limit(limit).offset(offset)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_track(r) for r in rows]

    async def count(self) -> int:
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(tracks)
        )
        return row[0] if row else 0

    async def list_initial_letters(self) -> list[tuple[str, int]]:
        """Return (letter, count) pairs for track titles, A-Z + # for non-alpha."""
        letter = sa.case(
            (sa.func.upper(sa.func.substr(tracks.c.title, 1, 1)).between("A", "Z"),
             sa.func.upper(sa.func.substr(tracks.c.title, 1, 1))),
            else_="#",
        ).label("letter")
        stmt = (
            sa.select(letter, sa.func.count().label("cnt"))
            .group_by(letter)
            .order_by(letter)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [(r["letter"], r["cnt"]) for r in rows]

    async def count_by_letter(self, letter: str) -> int:
        first_char = sa.func.upper(sa.func.substr(tracks.c.title, 1, 1))
        if letter == "#":
            where = ~first_char.between("A", "Z")
        else:
            where = first_char == letter.upper()
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(tracks).where(where)
        )
        return row[0] if row else 0

    async def list_by_letter(self, letter: str, limit: int = 500, offset: int = 0) -> list[Track]:
        first_char = sa.func.upper(sa.func.substr(tracks.c.title, 1, 1))
        if letter == "#":
            where = ~first_char.between("A", "Z")
        else:
            where = first_char == letter.upper()
        rows = await self._db.sa_fetchall(
            self._track_select().where(where)
            .order_by(tracks.c.title)
            .limit(limit).offset(offset)
        )
        return [_row_to_track(r) for r in rows]

    async def list_random(self, limit: int = 5000) -> list[Track]:
        """Return up to *limit* tracks in random order."""
        stmt = (
            self._track_select()
            .order_by(sa.func.random())
            .limit(limit)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_track(r) for r in rows]

    async def search_random(self, query: str, limit: int = 5000) -> list[Track]:
        """Return up to *limit* tracks matching *query* in random order."""
        where_clause = self._db.fts.search_where("tracks", query)
        folded = fold_accents(query)
        like_folded = f"%{folded}%"
        fts_query = sanitize_fts_query(query) + "*" if self._db.engine_name == "sqlite" else query
        if self._db.engine_name in ("postgres", "postgresql"):
            accent_fallback = sa.text("unaccent(tracks.title) ILIKE :like_folded")
        else:
            accent_fallback = sa.text("fold_accents(tracks.title) LIKE :like_folded")
        stmt = (
            self._track_select()
            .where(sa.or_(where_clause, accent_fallback))
            .order_by(sa.func.random())
            .limit(limit)
            .params(fts_query=fts_query, like_folded=like_folded)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_track(r) for r in rows]

    async def list_random_by_genre(self, genre: str, limit: int = 5000) -> list[Track]:
        """Return up to *limit* tracks matching *genre* in random order."""
        stmt = (
            self._track_select()
            .where(tracks.c.genre.ilike(f"%{genre}%"))
            .order_by(sa.func.random())
            .limit(limit)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_track(r) for r in rows]

    async def list_by_album(self, album_id: int) -> list[Track]:
        stmt = (
            self._track_select()
            .where(tracks.c.album_id == album_id)
            .order_by(tracks.c.disc_number, tracks.c.track_number)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_track(r) for r in rows]

    async def list_by_artist(self, artist_id: int) -> list[Track]:
        stmt = (
            self._track_select()
            .where(tracks.c.artist_id == artist_id)
            .order_by(tracks.c.title)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_track(r) for r in rows]

    async def get_by_path(self, file_path: str) -> Optional[Track]:
        stmt = self._track_select().where(tracks.c.file_path == file_path)
        row = await self._db.sa_fetchone(stmt)
        return _row_to_track(row) if row else None

    async def get_multiple(self, track_ids: list[int]) -> list[Track]:
        if not track_ids:
            return []
        stmt = (
            self._track_select()
            .where(tracks.c.id.in_(track_ids))
        )
        rows = await self._db.sa_fetchall(stmt)
        # Preserve caller's ordering — SQL IN() doesn't guarantee order, but
        # /play uses tracks[start_index] so the request order must round-trip.
        # Otherwise tapping track #5 of an alphabetical Flutter list plays
        # track #5 in album order instead. (Bug reported by Jacques.)
        by_id = {r["id"]: r for r in rows}
        ordered = [by_id[tid] for tid in track_ids if tid in by_id]
        return [_row_to_track(r) for r in ordered]

    async def create(self, track: Track) -> int:
        if self._db.engine_name == "postgres":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        stmt = dialect_insert(tracks).values(
                title=track.title,
                album_id=track.album_id,
                artist_id=track.artist_id,
                artist_name=track.artist_name or "",
                album_title=track.album_title or "",
                disc_number=track.disc_number or 1,
                disc_subtitle=track.disc_subtitle,
                track_number=track.track_number or 0,
                duration_ms=track.duration_ms or 0,
                file_path=track.file_path,
                format=track.format,
                sample_rate=track.sample_rate,
                bit_depth=track.bit_depth,
                channels=track.channels or 2,
                source=track.source or "local",
                source_id=track.source_id,
        ).on_conflict_do_nothing()
        result = await self._db.sa_execute(stmt)
        return result.lastrowid

    async def update(self, track: Track) -> None:
        await self._db.sa_execute(
            tracks.update().where(tracks.c.id == track.id).values(
                title=track.title,
                album_id=track.album_id,
                artist_id=track.artist_id,
                artist_name=track.artist_name or "",
                album_title=track.album_title or "",
                disc_number=track.disc_number,
                disc_subtitle=track.disc_subtitle,
                track_number=track.track_number,
                duration_ms=track.duration_ms,
                format=track.format,
                sample_rate=track.sample_rate,
                bit_depth=track.bit_depth,
                updated_at=sa.func.now(),
            )
        )

    async def delete(self, track_id: int) -> None:
        await self._db.sa_execute(
            tracks.delete().where(tracks.c.id == track_id)
        )

    async def search(self, query: str, limit: int = 50) -> list[Track]:
        where_clause = self._db.fts.search_where("tracks", query)
        folded = fold_accents(query)
        like_folded = f"%{folded}%"
        fts_query = sanitize_fts_query(query) + "*" if self._db.engine_name == "sqlite" else query
        if self._db.engine_name in ("postgres", "postgresql"):
            accent_fallback = sa.text("unaccent(tracks.title) ILIKE :like_folded")
        else:
            accent_fallback = sa.text("fold_accents(tracks.title) LIKE :like_folded")
        stmt = (
            self._track_select()
            .where(sa.or_(where_clause, accent_fallback))
            .limit(limit)
            .params(fts_query=fts_query, like_folded=like_folded)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_track(r) for r in rows]

    async def count_without_artist(self) -> int:
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(tracks).where(
                tracks.c.artist_id.is_(None)
            )
        )
        return row[0] if row else 0

    async def count_by_root(self, root_dir: str) -> int:
        prefix = root_dir.replace("\\", "/").rstrip("/") + "/"
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(tracks).where(
                tracks.c.file_path.like(prefix + "%")
            )
        )
        return row[0] if row else 0

    async def list_by_directory(self, directory: str) -> list[Track]:
        """Return tracks directly in a directory (not in subdirectories)."""
        prefix = directory.replace("\\", "/").rstrip("/") + "/"
        stmt = (
            self._track_select()
            .where(tracks.c.file_path.like(prefix + "%"))
            .where(~tracks.c.file_path.like(prefix + "%/%"))
            .order_by(tracks.c.file_path)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_track(r) for r in rows]

    async def list_subdirectories(self, directory: str) -> list[dict]:
        """Return immediate subdirectories with recursive track counts."""
        prefix = directory.replace("\\", "/").rstrip("/") + "/"
        prefix_len = len(prefix) + 1  # SQL SUBSTR is 1-based

        if self._db.engine_name == "postgres":
            rows = await self._db.fetchall(
                """SELECT
                    SPLIT_PART(SUBSTR(file_path, ?), '/', 1) AS dir_name,
                    COUNT(*) AS track_count
                   FROM tracks
                   WHERE file_path LIKE ?
                   AND LENGTH(file_path) > ?
                   AND POSITION('/' IN SUBSTR(file_path, ?)) > 0
                   GROUP BY dir_name
                   ORDER BY dir_name""",
                (prefix_len, prefix + "%", len(prefix), prefix_len),
            )
        else:
            # SQLite: use INSTR instead of POSITION, substr + instr for SPLIT_PART
            rows = await self._db.fetchall(
                """SELECT
                    CASE
                        WHEN INSTR(SUBSTR(file_path, ?), '/') > 0
                        THEN SUBSTR(SUBSTR(file_path, ?), 1, INSTR(SUBSTR(file_path, ?), '/') - 1)
                        ELSE SUBSTR(file_path, ?)
                    END AS dir_name,
                    COUNT(*) AS track_count
                   FROM tracks
                   WHERE file_path LIKE ?
                   AND LENGTH(file_path) > ?
                   AND INSTR(SUBSTR(file_path, ?), '/') > 0
                   GROUP BY dir_name
                   ORDER BY dir_name""",
                (prefix_len, prefix_len, prefix_len, prefix_len, prefix + "%", len(prefix), prefix_len),
            )
        return [
            {"name": r["dir_name"], "path": prefix + r["dir_name"], "track_count": r["track_count"]}
            for r in rows
        ]

    async def all_paths(self) -> set[str]:
        rows = await self._db.sa_fetchall(
            sa.select(tracks.c.file_path).where(tracks.c.file_path.isnot(None))
        )
        return {r["file_path"] for r in rows}

    async def update_waveform(self, track_id: int, waveform_data: str) -> None:
        await self._db.sa_execute(
            tracks.update().where(tracks.c.id == track_id).values(
                waveform_data=waveform_data,
                waveform_generated_at=sa.func.now(),
            )
        )

    async def update_bpm(self, track_id: int, bpm: float) -> None:
        await self._db.sa_execute(
            tracks.update().where(tracks.c.id == track_id).values(bpm=bpm)
        )

    async def delete_by_paths(self, paths: set[str]) -> int:
        if not paths:
            return 0
        result = await self._db.sa_execute(
            tracks.delete().where(tracks.c.file_path.in_(list(paths)))
        )
        return result.rowcount

    # Legacy-compat aliases and missing methods
    async def get_all_paths(self) -> set[str]:
        """Alias for all_paths() — legacy repo compat."""
        return await self.all_paths()

    async def delete_by_path(self, file_path: str) -> None:
        """Delete a single track by file_path."""
        await self._db.sa_execute(
            tracks.delete().where(tracks.c.file_path == file_path)
        )

    async def get_by_source(self, source: str, source_id: str) -> Optional[Track]:
        row = await self._db.sa_fetchone(
            self._track_select().where(
                sa.and_(tracks.c.source == source, tracks.c.source_id == source_id)
            )
        )
        return _row_to_track(row) if row else None

    async def get_by_sources(self, items: list[tuple[str, str]]) -> dict[tuple[str, str], Track]:
        """Batch lookup tracks by (source, source_id) pairs."""
        if not items:
            return {}
        conditions = [
            sa.and_(tracks.c.source == src, tracks.c.source_id == sid)
            for src, sid in items
        ]
        rows = await self._db.sa_fetchall(
            self._track_select().where(sa.or_(*conditions))
        )
        result: dict[tuple[str, str], Track] = {}
        for r in rows:
            t = _row_to_track(r)
            result[(t.source, t.source_id)] = t
        return result

    async def get_mtime(self, file_path: str) -> Optional[float]:
        row = await self._db.sa_fetchone(
            sa.select(tracks.c.file_mtime).where(tracks.c.file_path == file_path)
        )
        return row[0] if row else None

    async def update_mtime(self, file_path: str, mtime: float) -> None:
        await self._db.sa_execute(
            tracks.update().where(tracks.c.file_path == file_path).values(file_mtime=mtime)
        )

    async def get_file_size(self, file_path: str) -> int | None:
        row = await self._db.sa_fetchone(
            sa.select(tracks.c.file_size).where(tracks.c.file_path == file_path)
        )
        return row[0] if row else None

    async def update_file_size(self, file_path: str, file_size: int) -> None:
        await self._db.sa_execute(
            tracks.update().where(tracks.c.file_path == file_path).values(file_size=file_size)
        )

    async def update_mtime_and_size(self, file_path: str, mtime: float, file_size: int) -> None:
        await self._db.sa_execute(
            tracks.update().where(tracks.c.file_path == file_path).values(
                file_mtime=mtime, file_size=file_size,
            )
        )

    async def update_audio_hash(self, file_path: str, audio_hash: str) -> None:
        await self._db.sa_execute(
            tracks.update().where(tracks.c.file_path == file_path).values(audio_hash=audio_hash)
        )

    async def find_by_audio_hash(self, audio_hash: str) -> Optional[Track]:
        row = await self._db.sa_fetchone(
            self._track_select().where(tracks.c.audio_hash == audio_hash).limit(1)
        )
        return _row_to_track(row) if row else None

    async def update_loudness(self, track_id: int, lufs: float) -> None:
        await self._db.sa_execute(
            tracks.update().where(tracks.c.id == track_id).values(loudness_lufs=lufs)
        )

    async def deduplicate(self) -> int:
        """Remove duplicate tracks (same audio_hash, file_size AND duration_ms), keeping the lowest id.

        Re-targets playlist_tracks and play_queue references before deleting.
        Uses raw SQL for the complex subqueries.
        """
        _dup_join = """JOIN (
                        SELECT audio_hash, file_size, duration_ms
                          FROM tracks
                         WHERE album_id IS NOT NULL AND audio_hash IS NOT NULL
                         GROUP BY audio_hash, file_size, duration_ms
                        HAVING COUNT(*) > 1
                    ) d ON t.audio_hash = d.audio_hash AND t.file_size = d.file_size AND t.duration_ms = d.duration_ms"""

        _canonical_subquery = """SELECT MIN(id) FROM tracks
                       WHERE audio_hash = (SELECT audio_hash FROM tracks WHERE id = {table}.track_id)
                         AND file_size = (SELECT file_size FROM tracks WHERE id = {table}.track_id)
                         AND duration_ms = (SELECT duration_ms FROM tracks WHERE id = {table}.track_id)
                         AND audio_hash IS NOT NULL AND album_id IS NOT NULL"""

        # 1. Re-target playlist_tracks at the canonical (min-id) row.
        await self._db.execute(
            f"""UPDATE playlist_tracks
                  SET track_id = ({_canonical_subquery.format(table='playlist_tracks')})
                WHERE track_id IN (
                    SELECT t.id FROM tracks t {_dup_join}
                )""",
        )
        # 2. Same for play_queue.
        await self._db.execute(
            f"""UPDATE play_queue
                  SET track_id = ({_canonical_subquery.format(table='play_queue')})
                WHERE track_id IN (
                    SELECT t.id FROM tracks t {_dup_join}
                )""",
        )
        # 3. Delete the non-canonical duplicates.
        cursor = await self._db.execute(
            """DELETE FROM tracks WHERE id NOT IN (
                SELECT MIN(id) FROM tracks
                WHERE album_id IS NOT NULL AND audio_hash IS NOT NULL
                GROUP BY audio_hash, file_size, duration_ms
            ) AND id IN (
                SELECT t.id FROM tracks t
                JOIN (
                    SELECT audio_hash, file_size, duration_ms
                    FROM tracks WHERE album_id IS NOT NULL AND audio_hash IS NOT NULL
                    GROUP BY audio_hash, file_size, duration_ms
                    HAVING COUNT(*) > 1
                ) d ON t.audio_hash = d.audio_hash AND t.file_size = d.file_size AND t.duration_ms = d.duration_ms
            )""",
        )
        return cursor.rowcount

    async def list_recent_duplicates(self, limit: int = 50) -> list[dict]:
        rows = await self._db.fetchall(
            """SELECT audio_hash, file_size, COUNT(*) as cnt,
                      GROUP_CONCAT(file_path, ' | ') as paths
               FROM tracks
               WHERE audio_hash IS NOT NULL AND album_id IS NOT NULL
               GROUP BY audio_hash, file_size, duration_ms
               HAVING COUNT(*) > 1
               LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]


# ===================================================================
# ZoneRepo — SA Core
# ===================================================================

class SAZoneRepo:
    def __init__(self, db: SADatabase) -> None:
        self._db = db

    async def get(self, zone_id: int) -> dict | None:
        row = await self._db.sa_fetchone(
            sa.select(zones).where(zones.c.id == zone_id)
        )
        return dict(row) if row else None

    async def list(self) -> list[dict]:
        rows = await self._db.sa_fetchall(
            sa.select(zones).order_by(zones.c.id)
        )
        return [dict(r) for r in rows]

    async def create(self, name: str | None = None,
                     output_type: str | None = None,
                     output_device_id: str | None = None,
                     **kwargs) -> int:
        values = dict(kwargs)
        if name is not None:
            values["name"] = name
        if output_type is not None:
            values["output_type"] = output_type
        if output_device_id is not None:
            values["output_device_id"] = output_device_id
        result = await self._db.sa_execute(
            zones.insert().values(**values)
        )
        return result.lastrowid

    async def update(self, zone_id: int, **kwargs) -> None:
        await self._db.sa_execute(
            zones.update().where(zones.c.id == zone_id).values(**kwargs)
        )

    async def delete(self, zone_id: int) -> None:
        await self._db.sa_execute(
            zones.delete().where(zones.c.id == zone_id)
        )


# ===================================================================
# PlaylistRepo — SA Core
# ===================================================================

class SAPlaylistRepo:
    def __init__(self, db: SADatabase) -> None:
        self._db = db

    async def get(self, playlist_id: int) -> Optional[Playlist]:
        tc = (
            sa.select(sa.func.count().label("tc"))
            .select_from(playlist_tracks)
            .where(playlist_tracks.c.playlist_id == playlists.c.id)
            .correlate(playlists)
            .scalar_subquery()
        )
        stmt = sa.select(playlists, tc.label("track_count")).where(playlists.c.id == playlist_id)
        row = await self._db.sa_fetchone(stmt)
        return _row_to_playlist(row) if row else None

    async def list(self, limit: int = 500, offset: int = 0) -> list[Playlist]:
        tc = (
            sa.select(sa.func.count().label("tc"))
            .select_from(playlist_tracks)
            .where(playlist_tracks.c.playlist_id == playlists.c.id)
            .correlate(playlists)
            .scalar_subquery()
        )
        stmt = sa.select(playlists, tc.label("track_count")).order_by(playlists.c.name).limit(limit).offset(offset)
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_playlist(r) for r in rows]

    async def create(self, name: str, description: str = None) -> int:
        result = await self._db.sa_execute(
            playlists.insert().values(name=name, description=description)
        )
        return result.lastrowid

    async def update(self, playlist_id: int, name: Optional[str] = None,
                     description: Optional[str] = None) -> None:
        values = {}
        if name is not None:
            values["name"] = name
        if description is not None:
            values["description"] = description
        if not values:
            return
        values["updated_at"] = sa.func.now()
        await self._db.sa_execute(
            playlists.update().where(playlists.c.id == playlist_id).values(**values)
        )

    async def delete(self, playlist_id: int) -> None:
        await self._db.sa_execute(
            playlists.delete().where(playlists.c.id == playlist_id)
        )

    async def get_tracks(self, playlist_id: int) -> list[Track]:
        # The tracks table has its own denormalised album_title /
        # artist_name / cover_path columns; we MUST label the JOINed
        # values with the `_resolved` suffix recognised by
        # _row_to_track, otherwise asyncpg/SA refuses to map the result
        # set with `Ambiguous column name 'album_title' …`.
        stmt = (
            sa.select(
                tracks,
                albums.c.title.label("album_title_resolved"),
                artists.c.name.label("artist_name_resolved"),
                albums.c.cover_path.label("cover_path_resolved"),
            )
            .select_from(
                playlist_tracks
                .join(tracks, playlist_tracks.c.track_id == tracks.c.id)
                .outerjoin(albums, tracks.c.album_id == albums.c.id)
                .outerjoin(artists, tracks.c.artist_id == artists.c.id)
            )
            .where(playlist_tracks.c.playlist_id == playlist_id)
            .order_by(playlist_tracks.c.position)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_track(r) for r in rows]

    async def add_track(self, playlist_id: int, track_id: int, position: int = None) -> bool:
        """Add a track to a playlist, skipping if already present.

        Returns True if inserted, False if the track was already in the playlist.
        """
        existing = await self._db.sa_fetchone(
            sa.select(sa.func.count())
            .select_from(playlist_tracks)
            .where(
                sa.and_(
                    playlist_tracks.c.playlist_id == playlist_id,
                    playlist_tracks.c.track_id == track_id,
                )
            )
        )
        if existing and existing[0] > 0:
            return False

        if position is None:
            row = await self._db.sa_fetchone(
                sa.select(sa.func.coalesce(sa.func.max(playlist_tracks.c.position), -1) + 1)
                .where(playlist_tracks.c.playlist_id == playlist_id)
            )
            position = row[0] if row else 0
        await self._db.sa_execute(
            playlist_tracks.insert().values(
                playlist_id=playlist_id, track_id=track_id, position=position
            )
        )
        return True

    async def add_tracks(self, playlist_id: int, track_ids: list[int], position: int | None = None) -> list[int]:
        """Add several tracks to a playlist, skipping any already present.

        Returns the list of track_ids actually inserted (deduplicated against
        the playlist + the incoming list itself, caller order preserved).
        """
        if not track_ids:
            return []

        existing_rows = await self._db.sa_fetchall(
            sa.select(playlist_tracks.c.track_id).where(
                playlist_tracks.c.playlist_id == playlist_id
            )
        )
        existing = {r[0] for r in existing_rows}
        seen_in_batch: set[int] = set()
        new_ids: list[int] = []
        for tid in track_ids:
            if tid in existing or tid in seen_in_batch:
                continue
            seen_in_batch.add(tid)
            new_ids.append(tid)
        if not new_ids:
            return []

        if position is not None:
            await self._db.sa_execute(
                playlist_tracks.update()
                .where(
                    sa.and_(
                        playlist_tracks.c.playlist_id == playlist_id,
                        playlist_tracks.c.position >= position,
                    )
                )
                .values(position=playlist_tracks.c.position + len(new_ids))
            )
        else:
            row = await self._db.sa_fetchone(
                sa.select(sa.func.coalesce(sa.func.max(playlist_tracks.c.position), -1) + 1)
                .where(playlist_tracks.c.playlist_id == playlist_id)
            )
            position = row[0] if row else 0

        await self._db.sa_execute(
            playlist_tracks.insert().values(
                [
                    {"playlist_id": playlist_id, "track_id": tid, "position": position + i}
                    for i, tid in enumerate(new_ids)
                ]
            )
        )
        return new_ids

    async def reorder_tracks(self, playlist_id: int, track_ids: list[int]) -> None:
        await self._db.sa_execute(
            playlist_tracks.delete().where(playlist_tracks.c.playlist_id == playlist_id)
        )
        if track_ids:
            await self._db.sa_execute(
                playlist_tracks.insert().values(
                    [
                        {"playlist_id": playlist_id, "track_id": tid, "position": i}
                        for i, tid in enumerate(track_ids)
                    ]
                )
            )

    async def remove_track_by_id(self, playlist_id: int, track_id: int) -> None:
        """Remove the first occurrence of track_id from the playlist and
        compact remaining positions. Used by `DELETE
        /playlists/{playlist_id}/tracks/{track_id}` where the route key
        is the track id, not its position in the playlist."""
        row = await self._db.sa_fetchone(
            sa.select(playlist_tracks.c.position).where(
                sa.and_(
                    playlist_tracks.c.playlist_id == playlist_id,
                    playlist_tracks.c.track_id == track_id,
                )
            )
        )
        if not row:
            return
        await self._db.sa_execute(
            playlist_tracks.delete().where(
                sa.and_(
                    playlist_tracks.c.playlist_id == playlist_id,
                    playlist_tracks.c.track_id == track_id,
                )
            )
        )
        await self._db.sa_execute(
            playlist_tracks.update()
            .where(
                sa.and_(
                    playlist_tracks.c.playlist_id == playlist_id,
                    playlist_tracks.c.position > row[0],
                )
            )
            .values(position=playlist_tracks.c.position - 1)
        )

    async def remove_track(self, playlist_id: int, position: int) -> None:
        await self._db.sa_execute(
            playlist_tracks.delete().where(
                sa.and_(
                    playlist_tracks.c.playlist_id == playlist_id,
                    playlist_tracks.c.position == position,
                )
            )
        )


# ===================================================================
# RadioStationRepo — SA Core
# ===================================================================

class SARadioStationRepo:
    def __init__(self, db: SADatabase) -> None:
        self._db = db

    async def list(
        self,
        limit: int = 100,
        offset: int = 0,
        genre: str | None = None,
        favorite: bool | None = None,
    ) -> list[RadioStation]:
        stmt = sa.select(radio_stations).order_by(radio_stations.c.name)
        if genre:
            stmt = stmt.where(radio_stations.c.genre == genre)
        if favorite is not None:
            stmt = stmt.where(radio_stations.c.favorite == favorite)
        stmt = stmt.limit(limit).offset(offset)
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_radio_station(r) for r in rows]

    async def count(self) -> int:
        row = await self._db.sa_fetchone(sa.select(sa.func.count()).select_from(radio_stations))
        return row[0] if row else 0

    async def get_by_url(self, stream_url: str) -> RadioStation | None:
        row = await self._db.sa_fetchone(
            sa.select(radio_stations).where(radio_stations.c.stream_url == stream_url)
        )
        return _row_to_radio_station(row) if row else None

    async def get(self, station_id: int) -> Optional[RadioStation]:
        row = await self._db.sa_fetchone(
            sa.select(radio_stations).where(radio_stations.c.id == station_id)
        )
        return _row_to_radio_station(row) if row else None

    async def create(self, station: RadioStationCreate) -> int:
        result = await self._db.sa_execute(
            radio_stations.insert().values(
                name=station.name,
                stream_url=station.stream_url,
                logo_url=station.logo_url,
                genre=station.genre,
                tags=station.tags,
                codec=station.codec,
                country=station.country,
                homepage_url=station.homepage_url,
            )
        )
        return result.lastrowid

    async def update(self, station_id: int, **kwargs) -> None:
        kwargs["updated_at"] = sa.func.now()
        await self._db.sa_execute(
            radio_stations.update().where(radio_stations.c.id == station_id).values(**kwargs)
        )

    async def delete(self, station_id: int) -> None:
        await self._db.sa_execute(
            radio_stations.delete().where(radio_stations.c.id == station_id)
        )

    async def toggle_favorite(self, station_id: int) -> bool:
        row = await self._db.sa_fetchone(
            sa.select(radio_stations.c.favorite).where(radio_stations.c.id == station_id)
        )
        if not row:
            return False
        new_val = not bool(row["favorite"])
        await self._db.sa_execute(
            radio_stations.update().where(radio_stations.c.id == station_id)
            .values(favorite=new_val)
        )
        return new_val


# ===================================================================
# PlayQueueRepo — SA Core
# ===================================================================

class SAPlayQueueRepo:
    def __init__(self, db: SADatabase) -> None:
        self._db = db

    def _queue_select(self):
        """Queue items with track, album, and artist info."""
        return (
            sa.select(
                play_queue,
                tracks.c.title, tracks.c.file_path, tracks.c.duration_ms,
                tracks.c.format, tracks.c.sample_rate, tracks.c.bit_depth,
                tracks.c.channels, tracks.c.source, tracks.c.source_id,
                albums.c.title.label("album_title"),
                artists.c.name.label("artist_name"),
                albums.c.cover_path.label("cover_path"),
            )
            .join(tracks, play_queue.c.track_id == tracks.c.id)
            .outerjoin(albums, tracks.c.album_id == albums.c.id)
            .outerjoin(artists, tracks.c.artist_id == artists.c.id)
        )

    async def get_queue(self, zone_id: int) -> list[dict]:
        stmt = (
            self._queue_select()
            .where(play_queue.c.zone_id == zone_id)
            .order_by(play_queue.c.position)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [dict(r) for r in rows]

    async def get_current(self, zone_id: int) -> dict | None:
        stmt = (
            self._queue_select()
            .where(sa.and_(
                play_queue.c.zone_id == zone_id,
                play_queue.c.is_current == True,
            ))
        )
        row = await self._db.sa_fetchone(stmt)
        return dict(row) if row else None

    async def set_queue(self, zone_id: int, track_ids: list[int]) -> None:
        async with self._db.sa_engine.begin() as conn:
            await conn.execute(
                play_queue.delete().where(play_queue.c.zone_id == zone_id)
            )
            for i, track_id in enumerate(track_ids):
                await conn.execute(
                    play_queue.insert().values(
                        zone_id=zone_id, track_id=track_id,
                        position=i, is_current=(i == 0),
                    )
                )

    async def add_tracks(self, zone_id: int, track_ids: list[int], position: int | None = None) -> None:
        if position is not None:
            await self._db.sa_execute(
                play_queue.update()
                .where(sa.and_(
                    play_queue.c.zone_id == zone_id,
                    play_queue.c.position >= position,
                ))
                .values(position=play_queue.c.position + len(track_ids))
            )
        else:
            row = await self._db.sa_fetchone(
                sa.select(
                    sa.func.coalesce(sa.func.max(play_queue.c.position), -1) + 1
                ).where(play_queue.c.zone_id == zone_id)
            )
            position = row[0] if row else 0

        async with self._db.sa_engine.begin() as conn:
            for i, track_id in enumerate(track_ids):
                await conn.execute(
                    play_queue.insert().values(
                        zone_id=zone_id, track_id=track_id,
                        position=position + i,
                    )
                )

    async def set_current(self, zone_id: int, position: int) -> None:
        async with self._db.sa_engine.begin() as conn:
            await conn.execute(
                play_queue.update()
                .where(play_queue.c.zone_id == zone_id)
                .values(is_current=False)
            )
            await conn.execute(
                play_queue.update()
                .where(sa.and_(
                    play_queue.c.zone_id == zone_id,
                    play_queue.c.position == position,
                ))
                .values(is_current=True)
            )

    async def clear(self, zone_id: int) -> None:
        await self._db.sa_execute(
            play_queue.delete().where(play_queue.c.zone_id == zone_id)
        )

    async def count(self, zone_id: int) -> int:
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(play_queue)
            .where(play_queue.c.zone_id == zone_id)
        )
        return row[0] if row else 0


# ===================================================================
# RadioFavoriteRepo — SA Core
# ===================================================================

class SARadioFavoriteRepo:
    def __init__(self, db: SADatabase) -> None:
        self._db = db

    async def list(self, limit: int = 200, offset: int = 0) -> list[dict]:
        stmt = (
            sa.select(radio_favorites)
            .order_by(radio_favorites.c.saved_at.desc())
            .limit(limit).offset(offset)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [dict(r) for r in rows]

    async def count(self) -> int:
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(radio_favorites)
        )
        return row[0] if row else 0

    async def save(self, title: str, artist: str, station_name: str = "",
                   cover_url: str | None = None, stream_url: str | None = None) -> dict | None:
        if not title:
            return None
        try:
            # Use raw SQL for ON CONFLICT since SA dialect handling varies
            await self._db.execute(
                """INSERT INTO radio_favorites
                   (title, artist, station_name, cover_url, stream_url)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (title, artist) DO NOTHING""",
                (title, artist, station_name, cover_url, stream_url),
            )
            row = await self._db.sa_fetchone(
                sa.select(radio_favorites).where(
                    sa.and_(
                        radio_favorites.c.title == title,
                        radio_favorites.c.artist == artist,
                    )
                )
            )
            return dict(row) if row else None
        except Exception:
            return None

    async def is_favorite(self, title: str, artist: str) -> bool:
        row = await self._db.sa_fetchone(
            sa.select(sa.literal(1)).select_from(radio_favorites).where(
                sa.and_(
                    radio_favorites.c.title == title,
                    radio_favorites.c.artist == artist,
                )
            )
        )
        return row is not None

    async def delete(self, fav_id: int) -> None:
        await self._db.sa_execute(
            radio_favorites.delete().where(radio_favorites.c.id == fav_id)
        )

    async def clear(self) -> None:
        await self._db.sa_execute(radio_favorites.delete())

    async def export_csv(self) -> str:
        rows = await self._db.sa_fetchall(
            sa.select(
                radio_favorites.c.artist, radio_favorites.c.title,
                radio_favorites.c.station_name, radio_favorites.c.saved_at,
            ).order_by(radio_favorites.c.saved_at.desc())
        )
        lines = ["Artist,Title,Station,Date"]
        for r in rows:
            artist = str(r["artist"]).replace('"', '""')
            title = str(r["title"]).replace('"', '""')
            station = str(r["station_name"]).replace('"', '""')
            lines.append(f'"{artist}","{title}","{station}","{r["saved_at"]}"')
        return "\n".join(lines)


# ===================================================================
# PartyVoteRepo — SA Core
# ===================================================================

class SAPartyVoteRepo:
    def __init__(self, db: SADatabase) -> None:
        self._db = db

    async def increment(self, zone_id: int, position: int, title: str, artist: str | None) -> int:
        """Increment vote count. Create row if not exists. Return new count."""
        row = await self._db.sa_fetchone(
            sa.select(party_votes.c.id, party_votes.c.vote_count).where(
                sa.and_(
                    party_votes.c.zone_id == zone_id,
                    party_votes.c.queue_position == position,
                )
            )
        )
        if row:
            new_count = row["vote_count"] + 1
            await self._db.sa_execute(
                party_votes.update()
                .where(party_votes.c.id == row["id"])
                .values(vote_count=new_count, updated_at=sa.func.now())
            )
            return new_count
        else:
            await self._db.sa_execute(
                party_votes.insert().values(
                    zone_id=zone_id,
                    track_title=title,
                    track_artist=artist,
                    queue_position=position,
                    vote_count=1,
                )
            )
            return 1

    async def get_votes(self, zone_id: int) -> dict[int, int]:
        """Get all votes for a zone. Returns {position: count}."""
        rows = await self._db.sa_fetchall(
            sa.select(party_votes.c.queue_position, party_votes.c.vote_count)
            .where(party_votes.c.zone_id == zone_id)
        )
        return {r["queue_position"]: r["vote_count"] for r in rows}

    async def clear(self, zone_id: int) -> int:
        """Clear all votes for a zone."""
        result = await self._db.sa_execute(
            party_votes.delete().where(party_votes.c.zone_id == zone_id)
        )
        return result.rowcount if hasattr(result, 'rowcount') else 0

    async def swap_positions(self, zone_id: int, pos_a: int, pos_b: int) -> None:
        """Swap vote records for two queue positions."""
        row_a = await self._db.sa_fetchone(
            sa.select(party_votes.c.id, party_votes.c.vote_count).where(
                sa.and_(party_votes.c.zone_id == zone_id, party_votes.c.queue_position == pos_a)
            )
        )
        row_b = await self._db.sa_fetchone(
            sa.select(party_votes.c.id, party_votes.c.vote_count).where(
                sa.and_(party_votes.c.zone_id == zone_id, party_votes.c.queue_position == pos_b)
            )
        )
        if row_a and row_b:
            await self._db.sa_execute(
                party_votes.update().where(party_votes.c.id == row_a["id"])
                .values(queue_position=pos_b, updated_at=sa.func.now())
            )
            await self._db.sa_execute(
                party_votes.update().where(party_votes.c.id == row_b["id"])
                .values(queue_position=pos_a, updated_at=sa.func.now())
            )
        elif row_a:
            await self._db.sa_execute(
                party_votes.update().where(party_votes.c.id == row_a["id"])
                .values(queue_position=pos_b, updated_at=sa.func.now())
            )
        elif row_b:
            await self._db.sa_execute(
                party_votes.update().where(party_votes.c.id == row_b["id"])
                .values(queue_position=pos_a, updated_at=sa.func.now())
            )


# ===================================================================
# AlbumRatingRepo — SA Core
# ===================================================================

class SAAlbumRatingRepo:
    def __init__(self, db: SADatabase) -> None:
        self._db = db

    async def rate(self, album_id: int, rating: int, note: str | None = None, profile_id: int | None = None) -> dict:
        # Try update first, then insert
        existing = await self._db.sa_fetchone(
            sa.select(album_ratings.c.id).where(
                sa.and_(
                    album_ratings.c.album_id == album_id,
                    album_ratings.c.profile_id == profile_id if profile_id is not None
                    else album_ratings.c.profile_id.is_(None),
                )
            )
        )
        if existing:
            await self._db.sa_execute(
                album_ratings.update()
                .where(album_ratings.c.id == existing["id"])
                .values(rating=rating, note=note, updated_at=sa.func.now())
            )
        else:
            await self._db.sa_execute(
                album_ratings.insert().values(
                    album_id=album_id, profile_id=profile_id, rating=rating, note=note,
                )
            )
        return {"album_id": album_id, "rating": rating, "note": note}

    async def get(self, album_id: int, profile_id: int | None = None) -> dict | None:
        stmt = sa.select(album_ratings.c.rating, album_ratings.c.note).where(
            sa.and_(
                album_ratings.c.album_id == album_id,
                sa.or_(
                    album_ratings.c.profile_id == profile_id,
                    album_ratings.c.profile_id.is_(None),
                ),
            )
        )
        row = await self._db.sa_fetchone(stmt)
        if row:
            return {"album_id": album_id, "rating": row["rating"], "note": row["note"]}
        return None

    async def top_rated(self, limit: int = 20) -> list:
        stmt = (
            sa.select(
                album_ratings.c.album_id,
                albums.c.title,
                albums.c.artist_name,
                albums.c.cover_path,
                album_ratings.c.rating,
                album_ratings.c.note,
            )
            .join(albums, album_ratings.c.album_id == albums.c.id)
            .order_by(album_ratings.c.rating.desc(), album_ratings.c.updated_at.desc())
            .limit(limit)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [
            {"album_id": r["album_id"], "title": r["title"], "artist_name": r["artist_name"],
             "cover_path": r["cover_path"], "rating": r["rating"], "note": r["note"]}
            for r in rows
        ]


# ===================================================================
# TrackCreditRepo — SA Core
# ===================================================================

def _row_to_track_credit(row) -> TrackCredit:
    return TrackCredit(
        id=row["id"],
        track_id=row["track_id"],
        artist_id=row["artist_id"],
        artist_name=row["artist_name"],
        role=row["role"],
        instrument=row.get("instrument") if hasattr(row, "get") else row["instrument"],
        position=row["position"],
    )


class SATrackCreditRepo:
    def __init__(self, db: SADatabase) -> None:
        self._db = db

    async def list_by_track(self, track_id: int) -> list[TrackCredit]:
        rows = await self._db.sa_fetchall(
            sa.select(track_credits)
            .where(track_credits.c.track_id == track_id)
            .order_by(track_credits.c.position)
        )
        return [_row_to_track_credit(r) for r in rows]

    async def list_by_artist(self, artist_id: int) -> list[TrackCredit]:
        rows = await self._db.sa_fetchall(
            sa.select(track_credits)
            .where(track_credits.c.artist_id == artist_id)
            .order_by(track_credits.c.track_id, track_credits.c.position)
        )
        return [_row_to_track_credit(r) for r in rows]

    async def add(self, credit: TrackCredit) -> int:
        result = await self._db.sa_execute(
            track_credits.insert().values(
                track_id=credit.track_id,
                artist_id=credit.artist_id,
                artist_name=credit.artist_name,
                role=credit.role,
                instrument=credit.instrument,
                position=credit.position,
            )
        )
        return result.lastrowid

    async def add_many(self, credits: list[TrackCredit]) -> None:
        if not credits:
            return
        for c in credits:
            await self._db.sa_execute(
                track_credits.insert().values(
                    track_id=c.track_id,
                    artist_id=c.artist_id,
                    artist_name=c.artist_name,
                    role=c.role,
                    instrument=c.instrument,
                    position=c.position,
                )
            )

    async def delete_by_track(self, track_id: int) -> None:
        await self._db.sa_execute(
            track_credits.delete().where(track_credits.c.track_id == track_id)
        )

    async def update_instrument(self, credit_id: int, instrument: str) -> None:
        await self._db.sa_execute(
            track_credits.update()
            .where(track_credits.c.id == credit_id)
            .values(instrument=instrument)
        )

    async def get_instruments_for_artist(self, artist_id: int) -> list[str]:
        rows = await self._db.sa_fetchall(
            sa.select(sa.distinct(track_credits.c.instrument))
            .where(
                sa.and_(
                    track_credits.c.artist_id == artist_id,
                    track_credits.c.instrument.isnot(None),
                )
            )
            .order_by(track_credits.c.instrument)
        )
        return [r[0] for r in rows]


# ===================================================================
# PlaybackHistoryRepo — SA Core
# ===================================================================

class SAPlaybackHistoryRepo:
    def __init__(self, db: SADatabase) -> None:
        self._db = db

    async def record(self, track_id: int | None, zone_id: int | None,
                     track_title: str, artist_name: str | None, album_title: str | None,
                     cover_path: str | None, duration_ms: int | None,
                     listened_ms: int | None, source: str | None) -> None:
        await self._db.sa_execute(
            playback_history.insert().values(
                track_id=track_id,
                zone_id=zone_id,
                track_title=track_title,
                artist_name=artist_name,
                album_title=album_title,
                cover_path=cover_path,
                duration_ms=duration_ms,
                listened_ms=listened_ms,
                source=source,
                played_at=sa.func.now(),
            )
        )

    async def list_recent(self, limit: int = 50) -> list[dict]:
        rows = await self._db.sa_fetchall(
            sa.select(playback_history)
            .order_by(playback_history.c.played_at.desc())
            .limit(limit)
        )
        return [dict(r.items()) for r in rows]

    async def top_tracks(self, limit: int = 20) -> list[dict]:
        cover = sa.func.coalesce(playback_history.c.cover_path, albums.c.cover_path).label("cover_path")
        stmt = (
            sa.select(
                playback_history.c.track_title,
                playback_history.c.artist_name,
                playback_history.c.album_title,
                cover,
                sa.func.count().label("play_count"),
                sa.func.max(playback_history.c.played_at).label("last_played"),
            )
            .outerjoin(tracks, tracks.c.id == playback_history.c.track_id)
            .outerjoin(albums, albums.c.id == tracks.c.album_id)
            .where(playback_history.c.track_id.isnot(None))
            .group_by(
                playback_history.c.track_title,
                playback_history.c.artist_name,
                playback_history.c.album_title,
                cover,
            )
            .order_by(sa.desc("play_count"))
            .limit(limit)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [dict(r.items()) for r in rows]

    async def top_artists(self, limit: int = 20) -> list[dict]:
        stmt = (
            sa.select(
                playback_history.c.artist_name,
                sa.func.count().label("play_count"),
                sa.func.max(playback_history.c.played_at).label("last_played"),
            )
            .where(
                sa.and_(
                    playback_history.c.artist_name.isnot(None),
                    playback_history.c.artist_name != "",
                )
            )
            .group_by(playback_history.c.artist_name)
            .order_by(sa.desc("play_count"))
            .limit(limit)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [dict(r.items()) for r in rows]


# ===================================================================
# SmartPlaylistRepo — SA Core
# ===================================================================

class SASmartPlaylistRepo:
    """Smart playlists with dynamic rule-based track resolution."""

    def __init__(self, db: SADatabase) -> None:
        self._db = db

    async def list(self) -> list[dict]:
        rows = await self._db.sa_fetchall(
            sa.select(smart_playlists).order_by(smart_playlists.c.name)
        )
        return [dict(r.items()) for r in rows]

    async def get(self, sp_id: int) -> dict | None:
        row = await self._db.sa_fetchone(
            sa.select(smart_playlists).where(smart_playlists.c.id == sp_id)
        )
        return dict(row.items()) if row else None

    async def create(self, name: str, rules: str, match_mode: str = "all",
                     sort_by: str = "title", sort_order: str = "asc",
                     max_tracks: int = 200, description: str | None = None) -> int:
        result = await self._db.sa_execute(
            smart_playlists.insert().values(
                name=name,
                description=description,
                rules=rules,
                match_mode=match_mode,
                sort_by=sort_by,
                sort_order=sort_order,
                max_tracks=max_tracks,
                created_at=sa.func.now(),
                updated_at=sa.func.now(),
            )
        )
        return result.lastrowid

    async def update(self, sp_id: int, **kwargs) -> None:
        values = {}
        for key in ("name", "description", "rules", "match_mode",
                     "sort_by", "sort_order", "max_tracks"):
            if key in kwargs:
                values[key] = kwargs[key]
        if not values:
            return
        values["updated_at"] = sa.func.now()
        await self._db.sa_execute(
            smart_playlists.update()
            .where(smart_playlists.c.id == sp_id)
            .values(**values)
        )

    async def delete(self, sp_id: int) -> None:
        await self._db.sa_execute(
            smart_playlists.delete().where(smart_playlists.c.id == sp_id)
        )

    async def resolve_tracks(self, sp_id: int) -> list:
        """Resolve a smart playlist's rules into matching tracks."""
        import json
        sp = await self.get(sp_id)
        if not sp:
            return []

        rules = json.loads(sp["rules"]) if sp["rules"] else []
        match_mode = sp.get("match_mode", "all")  # "all" = AND, "any" = OR
        sort_by = sp.get("sort_by", "title")
        sort_order = sp.get("sort_order", "asc")
        max_tracks = sp.get("max_tracks", 5000)

        conditions = []

        for rule in rules:
            field = rule.get("field", "")
            op = rule.get("operator", "contains")
            value = rule.get("value", "")

            col_map = {
                "title": tracks.c.title,
                "artist": artists.c.name,
                "album": albums.c.title,
                "genre": albums.c.genre,
                "year": albums.c.year,
                "format": tracks.c.format,
                "sample_rate": tracks.c.sample_rate,
                "bit_depth": tracks.c.bit_depth,
                "source": tracks.c.source,
                "composer": tracks.c.composer,
            }
            col = col_map.get(field)
            if col is None:
                continue

            if op == "contains":
                conditions.append(col.like(f"%{value}%"))
            elif op == "equals":
                conditions.append(col == value)
            elif op == "not_equals":
                conditions.append(col != value)
            elif op == "greater_than":
                conditions.append(col > value)
            elif op == "less_than":
                conditions.append(col < value)
            elif op == "starts_with":
                conditions.append(col.like(f"{value}%"))
            elif op == "branch_of":
                from tune_server.library.genre_tree import expand_branch
                branch = expand_branch(str(value))
                if branch:
                    conditions.append(col.in_(sorted(branch)))

        where = None
        if conditions:
            if match_mode == "all":
                where = sa.and_(*conditions)
            else:
                where = sa.or_(*conditions)

        sort_col_map = {
            "title": tracks.c.title,
            "artist": artists.c.name,
            "album": albums.c.title,
            "year": albums.c.year,
            "duration": tracks.c.duration_ms,
            "track_number": tracks.c.track_number,
            "random": sa.func.random(),
        }
        order_col = sort_col_map.get(sort_by, tracks.c.title)
        order_dir = order_col.desc() if sort_order == "desc" else order_col.asc()

        stmt = (
            sa.select(
                tracks,
                albums.c.title.label("album_title"),
                artists.c.name.label("artist_name"),
                albums.c.cover_path.label("cover_path"),
            )
            .outerjoin(albums, tracks.c.album_id == albums.c.id)
            .outerjoin(artists, tracks.c.artist_id == artists.c.id)
        )
        if where is not None:
            stmt = stmt.where(where)
        stmt = stmt.order_by(order_dir).limit(max_tracks)

        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_track(r) for r in rows]


DEFAULT_SMART_PLAYLISTS: list[dict] = [
    {
        "name": "Hi-Res Tracks",
        "description": "Pistes ≥ 96 kHz",
        "rules": [{"field": "sample_rate", "operator": "greater_than", "value": 95999}],
        "sort_by": "title", "sort_order": "asc", "max_tracks": 200,
    },
    {
        "name": "Live",
        "description": "Pistes dont le titre contient « Live »",
        "rules": [{"field": "title", "operator": "contains", "value": "Live"}],
        "sort_by": "artist", "sort_order": "asc", "max_tracks": 200,
    },
    {
        "name": "Jazz",
        "description": "Pistes au genre Jazz",
        "rules": [{"field": "genre", "operator": "contains", "value": "Jazz"}],
        "sort_by": "artist", "sort_order": "asc", "max_tracks": 200,
    },
    {
        "name": "Random 50",
        "description": "50 pistes au hasard dans toute la bibliothèque",
        "rules": [],
        "sort_by": "random", "sort_order": "asc", "max_tracks": 50,
    },
]


async def seed_default_smart_playlists(repo: SASmartPlaylistRepo) -> int:
    """Seed default smart playlists on first server start. Idempotent --
    skip names that already exist (allows users to delete defaults
    they don't want without them coming back)."""
    import json
    existing = {row["name"] for row in await repo.list()}
    inserted = 0
    for spec in DEFAULT_SMART_PLAYLISTS:
        if spec["name"] in existing:
            continue
        try:
            await repo.create(
                name=spec["name"],
                rules=json.dumps(spec["rules"]),
                match_mode=spec.get("match_mode", "all"),
                sort_by=spec.get("sort_by", "title"),
                sort_order=spec.get("sort_order", "asc"),
                max_tracks=spec.get("max_tracks", 200),
                description=spec.get("description"),
            )
            inserted += 1
        except Exception:
            pass
    return inserted


# ===================================================================
# Full-Text Search — SA Core (aggregated)
# ===================================================================

async def sa_full_text_search(db: SADatabase, query: str, limit: int = 50) -> SearchResult:
    """Federated FTS across artists, albums, tracks — database independent."""
    artist_repo = SAArtistRepo(db)
    album_repo = SAAlbumRepo(db)
    track_repo = SATrackRepo(db)

    found_tracks = await track_repo.search(query, limit)
    found_albums = await album_repo.search(query, limit)
    found_artists = await artist_repo.search(query, limit)

    # Enrich: also fetch albums/tracks for matching artists
    seen_album_ids = {a.id for a in found_albums if a.id}
    seen_track_ids = {t.id for t in found_tracks if t.id}
    for artist in found_artists:
        if not artist.id:
            continue
        artist_albums = await album_repo.list_by_artist(artist.id)
        for al in artist_albums:
            if al.id and al.id not in seen_album_ids:
                found_albums.append(al)
                seen_album_ids.add(al.id)
        artist_tracks = await track_repo.list_by_artist(artist.id)
        for tr in artist_tracks:
            if tr.id and tr.id not in seen_track_ids:
                found_tracks.append(tr)
                seen_track_ids.add(tr.id)
        if len(found_albums) >= limit and len(found_tracks) >= limit:
            break

    return SearchResult(
        tracks=found_tracks[:limit],
        albums=found_albums[:limit],
        artists=found_artists,
    )


# Alias for backward compatibility with callers using the compat.py name
full_text_search = sa_full_text_search


# ---------------------------------------------------------------------------
# Short-name aliases — allows `from tune_server.db.sa_repository import AlbumRepo`
# so callers don't need to know whether they're using the SA or legacy version.
# ---------------------------------------------------------------------------

ArtistRepo = SAArtistRepo
AlbumRepo = SAAlbumRepo
TrackRepo = SATrackRepo
ZoneRepo = SAZoneRepo
PlaylistRepo = SAPlaylistRepo
PlayQueueRepo = SAPlayQueueRepo
RadioStationRepo = SARadioStationRepo
RadioFavoriteRepo = SARadioFavoriteRepo
PartyVoteRepo = SAPartyVoteRepo
AlbumRatingRepo = SAAlbumRatingRepo
TrackCreditRepo = SATrackCreditRepo
PlaybackHistoryRepo = SAPlaybackHistoryRepo
SmartPlaylistRepo = SASmartPlaylistRepo

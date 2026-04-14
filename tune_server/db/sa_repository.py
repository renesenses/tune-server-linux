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
    user_profiles, user_favorites,
)
from tune_server.models import Album, Artist, Playlist, RadioStation, RadioStationCreate, SearchResult, Track

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

    async def list(self, limit: int = 100, offset: int = 0) -> list[Artist]:
        rows = await self._db.sa_fetchall(
            sa.select(artists)
            .order_by(artists.c.sort_name, artists.c.name)
            .limit(limit).offset(offset)
        )
        return [_row_to_artist(r) for r in rows]

    async def count(self) -> int:
        row = await self._db.sa_fetchone(
            sa.select(sa.func.count()).select_from(artists)
        )
        return row[0] if row else 0

    async def list_initial_letters(self) -> list[tuple[str, int]]:
        letter = sa.case(
            (sa.func.upper(sa.func.substr(sa.func.coalesce(artists.c.sort_name, artists.c.name), 1, 1))
             .between("A", "Z"),
             sa.func.upper(sa.func.substr(sa.func.coalesce(artists.c.sort_name, artists.c.name), 1, 1))),
            else_="#",
        ).label("letter")
        stmt = (
            sa.select(letter, sa.func.count().label("cnt"))
            .group_by(letter)
            .order_by(letter)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [(r["letter"], r["cnt"]) for r in rows]

    async def list_by_letter(self, letter: str, limit: int = 500, offset: int = 0) -> list[Artist]:
        first_char = sa.func.upper(sa.func.substr(
            sa.func.coalesce(artists.c.sort_name, artists.c.name), 1, 1
        ))
        if letter == "#":
            where = ~first_char.between("A", "Z")
        else:
            where = first_char == letter.upper()
        rows = await self._db.sa_fetchall(
            sa.select(artists).where(where)
            .order_by(artists.c.sort_name, artists.c.name)
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
            )
        )
        return result.lastrowid

    async def get_or_create(self, name: str) -> Artist:
        existing = await self.get_by_name(name)
        if existing:
            return existing
        artist_id = await self.create(Artist(name=name, sort_name=name))
        return Artist(id=artist_id, name=name, sort_name=name)

    async def update(self, artist: Artist) -> None:
        await self._db.sa_execute(
            artists.update().where(artists.c.id == artist.id).values(
                name=artist.name,
                sort_name=artist.sort_name,
                musicbrainz_id=artist.musicbrainz_id,
                discogs_id=artist.discogs_id,
                bio=artist.bio,
                image_path=artist.image_path,
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
        """FTS search — uses the engine's FTS plugin, no branching."""
        where_clause = self._db.fts.search_where("artists", query)
        rank_clause = self._db.fts.search_rank("artists", query)

        # Build query with FTS plugin clauses
        fts_query = query + "*" if self._db.engine_name == "sqlite" else query
        stmt = (
            sa.select(artists)
            .where(where_clause)
            .order_by(rank_clause.desc())
            .limit(limit)
            .params(fts_query=fts_query)
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
        """Base SELECT with artist name and track quality stats."""
        # Track quality subquery — simple aggregation, no correlated subquery
        tq = (
            sa.select(
                tracks.c.album_id,
                sa.func.max(tracks.c.sample_rate).label("max_sample_rate"),
                sa.func.max(tracks.c.bit_depth).label("max_bit_depth"),
            )
            .where(tracks.c.album_id.isnot(None))
            .group_by(tracks.c.album_id)
            .subquery("tq")
        )
        # Dominant format as correlated scalar subquery on main albums table
        dominant_fmt = (
            sa.select(tracks.c.format)
            .where(tracks.c.album_id == albums.c.id)
            .where(tracks.c.format.isnot(None))
            .order_by(tracks.c.sample_rate.desc().nullslast(), tracks.c.bit_depth.desc().nullslast())
            .limit(1)
            .correlate(albums)
            .scalar_subquery()
            .label("dominant_format")
        )
        return (
            sa.select(
                albums,
                sa.func.coalesce(artists.c.name, albums.c.artist_name).label("artist_name_resolved"),
                tq.c.max_sample_rate,
                tq.c.max_bit_depth,
                dominant_fmt,
            )
            .outerjoin(artists, albums.c.artist_id == artists.c.id)
            .outerjoin(tq, tq.c.album_id == albums.c.id)
        )

    async def get(self, album_id: int) -> Optional[Album]:
        stmt = self._album_select().where(albums.c.id == album_id)
        row = await self._db.sa_fetchone(stmt)
        return _row_to_album(row) if row else None

    async def list(self, limit: int = 100, offset: int = 0,
                   quality: str | None = None, format: str | None = None,
                   sample_rate: int | None = None) -> list[Album]:
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

        stmt = stmt.order_by(albums.c.title).limit(limit).offset(offset)
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
            .order_by(albums.c.year.desc().nullslast(), albums.c.title)
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
                genre=album.genre,
                cover_path=album.cover_path,
                source=album.source or "local",
                source_id=album.source_id,
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
                updated_at=sa.func.now(),
            )
        )

    async def delete(self, album_id: int) -> None:
        await self._db.sa_execute(
            albums.delete().where(albums.c.id == album_id)
        )

    async def search(self, query: str, limit: int = 50) -> list[Album]:
        where_clause = self._db.fts.search_where("albums", query)
        fts_query = query + "*" if self._db.engine_name == "sqlite" else query
        stmt = (
            self._album_select()
            .where(where_clause)
            .limit(limit)
            .params(fts_query=fts_query)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_album(r) for r in rows]

    async def list_genres(self) -> list[dict]:
        stmt = (
            sa.select(albums.c.genre, sa.func.count().label("count"))
            .where(albums.c.genre.isnot(None), albums.c.genre != "")
            .group_by(albums.c.genre)
            .order_by(albums.c.genre)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [{"genre": r["genre"], "count": r["count"]} for r in rows]

    async def list_without_cover(self) -> list[Album]:
        stmt = (
            self._album_select()
            .where(sa.or_(albums.c.cover_path.is_(None), albums.c.cover_path == ""))
            .order_by(albums.c.title)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_album(r) for r in rows]


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
        return [_row_to_track(r) for r in rows]

    async def create(self, track: Track) -> int:
        result = await self._db.sa_execute(
            tracks.insert().values(
                title=track.title,
                album_id=track.album_id,
                artist_id=track.artist_id,
                disc_number=track.disc_number or 1,
                track_number=track.track_number or 0,
                duration_ms=track.duration_ms or 0,
                file_path=track.file_path,
                format=track.format,
                sample_rate=track.sample_rate,
                bit_depth=track.bit_depth,
                channels=track.channels or 2,
                source=track.source or "local",
                source_id=track.source_id,
            )
        )
        return result.lastrowid

    async def update(self, track: Track) -> None:
        await self._db.sa_execute(
            tracks.update().where(tracks.c.id == track.id).values(
                title=track.title,
                album_id=track.album_id,
                artist_id=track.artist_id,
                disc_number=track.disc_number,
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
        fts_query = query + "*" if self._db.engine_name == "sqlite" else query
        stmt = (
            self._track_select()
            .where(where_clause)
            .limit(limit)
            .params(fts_query=fts_query)
        )
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_track(r) for r in rows]

    async def all_paths(self) -> set[str]:
        rows = await self._db.sa_fetchall(
            sa.select(tracks.c.file_path).where(tracks.c.file_path.isnot(None))
        )
        return {r["file_path"] for r in rows}

    async def delete_by_paths(self, paths: set[str]) -> int:
        if not paths:
            return 0
        result = await self._db.sa_execute(
            tracks.delete().where(tracks.c.file_path.in_(list(paths)))
        )
        return result.rowcount


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

    async def create(self, **kwargs) -> int:
        result = await self._db.sa_execute(
            zones.insert().values(**kwargs)
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

    async def list(self) -> list[Playlist]:
        tc = (
            sa.select(sa.func.count().label("tc"))
            .select_from(playlist_tracks)
            .where(playlist_tracks.c.playlist_id == playlists.c.id)
            .correlate(playlists)
            .scalar_subquery()
        )
        stmt = sa.select(playlists, tc.label("track_count")).order_by(playlists.c.name)
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_playlist(r) for r in rows]

    async def create(self, name: str, description: str = None) -> int:
        result = await self._db.sa_execute(
            playlists.insert().values(name=name, description=description)
        )
        return result.lastrowid

    async def delete(self, playlist_id: int) -> None:
        await self._db.sa_execute(
            playlists.delete().where(playlists.c.id == playlist_id)
        )

    async def get_tracks(self, playlist_id: int) -> list[Track]:
        stmt = (
            sa.select(
                tracks,
                albums.c.title.label("album_title"),
                artists.c.name.label("artist_name"),
                albums.c.cover_path.label("cover_path"),
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

    async def add_track(self, playlist_id: int, track_id: int, position: int = None) -> None:
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

    async def list(self, genre: str = None) -> list[RadioStation]:
        stmt = sa.select(radio_stations).order_by(radio_stations.c.name)
        if genre:
            stmt = stmt.where(radio_stations.c.genre == genre)
        rows = await self._db.sa_fetchall(stmt)
        return [_row_to_radio_station(r) for r in rows]

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

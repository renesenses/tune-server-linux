"""Legacy repository classes that have no SQLAlchemy equivalent yet.

Kept as-is from repository.py — raw SQL, no logic changes.
Everything else migrated to sa_repository.py.
"""
from __future__ import annotations

from typing import Optional

import structlog

from tune_server.db.engine import Database
from tune_server.models import Playlist, RadioStation, SearchResult, Track, TrackCredit
from tune_server.utils import fold_accents, sanitize_fts_query

logger = structlog.get_logger()


def _row_get(row, key, default=None):
    # sqlite3.Row supports indexed access but not .get(); SA Row supports both.
    keys = row.keys() if hasattr(row, "keys") else []
    return row[key] if key in keys else default


def _row_to_artist(row):
    from tune_server.models import Artist
    return Artist(
        id=row["id"],
        name=row["name"],
        sort_name=_row_get(row, "sort_name"),
        musicbrainz_id=_row_get(row, "musicbrainz_id"),
        discogs_id=_row_get(row, "discogs_id"),
        bio=_row_get(row, "bio"),
        image_path=_row_get(row, "image_path"),
        image_source=_row_get(row, "image_source"),
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


def _row_to_album(row):
    from tune_server.models import Album
    keys = row.keys()
    sr = row["max_sample_rate"] if "max_sample_rate" in keys else None
    bd = row["max_bit_depth"] if "max_bit_depth" in keys else None
    fmt = row["dominant_format"] if "dominant_format" in keys else None
    return Album(
        id=row["id"],
        title=row["title"],
        artist_id=row["artist_id"],
        # Prefer the joined artist name over the denormalized al.artist_name.
        artist_name=(row["joined_artist_name"] if "joined_artist_name" in keys
                     else row["artist_name"] if "artist_name" in keys else None),
        year=row["year"],
        original_year=row["original_year"] if "original_year" in keys else None,
        release_date=row["release_date"] if "release_date" in keys else None,
        original_date=row["original_date"] if "original_date" in keys else None,
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
        bio=row["bio"] if "bio" in keys else None,
        label=row["label"] if "label" in keys else None,
        catalog_number=row["catalog_number"] if "catalog_number" in keys else None,
        musicbrainz_release_id=row["musicbrainz_release_id"] if "musicbrainz_release_id" in keys else None,
        musicbrainz_release_group_id=row["musicbrainz_release_group_id"] if "musicbrainz_release_group_id" in keys else None,
    )


def _row_to_track(row) -> Track:
    keys = row.keys()
    return Track(
        id=row["id"],
        title=row["title"],
        album_id=row["album_id"],
        album_title=row["album_title"] if "album_title" in keys else None,
        artist_id=row["artist_id"],
        artist_name=row["artist_name"] if "artist_name" in keys else None,
        disc_number=row["disc_number"],
        disc_subtitle=row["disc_subtitle"] if "disc_subtitle" in keys else None,
        track_number=row["track_number"],
        duration_ms=row["duration_ms"],
        file_path=row["file_path"],
        format=row["format"],
        sample_rate=row["sample_rate"],
        bit_depth=row["bit_depth"],
        channels=row["channels"],
        cover_path=row["cover_path"] if "cover_path" in keys else None,
        source=row["source"],
        source_id=row["source_id"],
        isrc=row["isrc"] if "isrc" in keys else None,
        bpm=row["bpm"] if "bpm" in keys else None,
        waveform_data=row["waveform_data"] if "waveform_data" in keys else None,
        waveform_generated_at=str(row["waveform_generated_at"]) if "waveform_generated_at" in keys and row["waveform_generated_at"] else None,
        loudness_lufs=row["loudness_lufs"] if "loudness_lufs" in keys else None,
        musicbrainz_recording_id=row["musicbrainz_recording_id"] if "musicbrainz_recording_id" in keys else None,
    )


# ---------------------------------------------------------------------------
# TrackCreditRepo — raw SQL (no SA equivalent yet)
# ---------------------------------------------------------------------------

def _row_to_track_credit(row) -> TrackCredit:
    return TrackCredit(
        id=row["id"],
        track_id=row["track_id"],
        artist_id=row["artist_id"],
        artist_name=row["artist_name"],
        role=row["role"],
        instrument=_row_get(row, "instrument"),
        position=row["position"],
    )


class TrackCreditRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def list_by_track(self, track_id: int) -> list[TrackCredit]:
        rows = await self._db.fetchall(
            "SELECT * FROM track_credits WHERE track_id = ? ORDER BY position",
            (track_id,),
        )
        return [_row_to_track_credit(r) for r in rows]

    async def list_by_artist(self, artist_id: int) -> list[TrackCredit]:
        rows = await self._db.fetchall(
            "SELECT * FROM track_credits WHERE artist_id = ? ORDER BY track_id, position",
            (artist_id,),
        )
        return [_row_to_track_credit(r) for r in rows]

    async def add(self, credit: TrackCredit) -> int:
        result = await self._db.execute(
            """INSERT INTO track_credits (track_id, artist_id, artist_name, role, instrument, position)
               VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
            (credit.track_id, credit.artist_id, credit.artist_name,
             credit.role, credit.instrument, credit.position),
        )
        await self._db.commit()
        return result.lastrowid

    async def add_many(self, credits: list[TrackCredit]) -> None:
        if not credits:
            return
        await self._db.executemany(
            """INSERT INTO track_credits (track_id, artist_id, artist_name, role, instrument, position)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (c.track_id, c.artist_id, c.artist_name, c.role, c.instrument, c.position)
                for c in credits
            ],
        )
        await self._db.commit()

    async def delete_by_track(self, track_id: int) -> None:
        await self._db.execute(
            "DELETE FROM track_credits WHERE track_id = ?", (track_id,)
        )
        await self._db.commit()

    async def update_instrument(self, credit_id: int, instrument: str) -> None:
        await self._db.execute(
            "UPDATE track_credits SET instrument = ? WHERE id = ?",
            (instrument, credit_id),
        )
        await self._db.commit()

    async def get_instruments_for_artist(self, artist_id: int) -> list[str]:
        rows = await self._db.fetchall(
            """SELECT DISTINCT instrument FROM track_credits
               WHERE artist_id = ? AND instrument IS NOT NULL
               ORDER BY instrument""",
            (artist_id,),
        )
        return [r["instrument"] for r in rows]


# ---------------------------------------------------------------------------
# PlaybackHistoryRepo — raw SQL (no SA equivalent yet)
# ---------------------------------------------------------------------------

class PlaybackHistoryRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(self, track_id: int | None, zone_id: int | None,
                     track_title: str, artist_name: str | None, album_title: str | None,
                     cover_path: str | None, duration_ms: int | None,
                     listened_ms: int | None, source: str | None) -> None:
        await self._db.execute(
            """INSERT INTO playback_history
               (track_id, zone_id, track_title, artist_name, album_title,
                cover_path, duration_ms, listened_ms, source, played_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (track_id, zone_id, track_title, artist_name, album_title,
             cover_path, duration_ms, listened_ms, source),
        )
        await self._db.commit()

    async def list_recent(self, limit: int = 50) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM playback_history ORDER BY played_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def top_tracks(self, limit: int = 20) -> list[dict]:
        rows = await self._db.fetchall(
            """SELECT ph.track_title, ph.artist_name, ph.album_title,
                      COALESCE(ph.cover_path, al.cover_path) as cover_path,
                      COUNT(*) as play_count, MAX(ph.played_at) as last_played
               FROM playback_history ph
               LEFT JOIN tracks t ON t.id = ph.track_id
               LEFT JOIN albums al ON al.id = t.album_id
               WHERE ph.track_id IS NOT NULL
               GROUP BY ph.track_title, ph.artist_name, ph.album_title,
                        COALESCE(ph.cover_path, al.cover_path)
               ORDER BY play_count DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def top_artists(self, limit: int = 20) -> list[dict]:
        rows = await self._db.fetchall(
            """SELECT artist_name, COUNT(*) as play_count, MAX(played_at) as last_played
               FROM playback_history
               WHERE artist_name IS NOT NULL AND artist_name != ''
               GROUP BY artist_name ORDER BY play_count DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# SmartPlaylistRepo — raw SQL (no SA equivalent yet)
# ---------------------------------------------------------------------------

class SmartPlaylistRepo:
    """Smart playlists with dynamic rule-based track resolution."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def list(self) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM smart_playlists ORDER BY name"
        )
        return [dict(r) for r in rows]

    async def get(self, sp_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM smart_playlists WHERE id = ?", (sp_id,)
        )
        return dict(row) if row else None

    async def create(self, name: str, rules: str, match_mode: str = "all",
                     sort_by: str = "title", sort_order: str = "asc",
                     max_tracks: int = 200, description: str | None = None) -> int:
        result = await self._db.execute(
            """INSERT INTO smart_playlists (name, description, rules, match_mode,
               sort_by, sort_order, max_tracks, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
               RETURNING id""",
            (name, description, rules, match_mode, sort_by, sort_order, max_tracks),
        )
        await self._db.commit()
        return result.lastrowid

    async def update(self, sp_id: int, **kwargs) -> None:
        fields = []
        values = []
        for key in ("name", "description", "rules", "match_mode",
                     "sort_by", "sort_order", "max_tracks"):
            if key in kwargs:
                fields.append(f"{key} = ?")
                values.append(kwargs[key])
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(sp_id)
        await self._db.execute(
            f"UPDATE smart_playlists SET {', '.join(fields)} WHERE id = ?",
            tuple(values),
        )
        await self._db.commit()

    async def delete(self, sp_id: int) -> None:
        await self._db.execute("DELETE FROM smart_playlists WHERE id = ?", (sp_id,))
        await self._db.commit()

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
        params = []

        for rule in rules:
            field = rule.get("field", "")
            op = rule.get("operator", "contains")
            value = rule.get("value", "")

            col_map = {
                "title": "t.title", "artist": "ar.name", "album": "al.title",
                "genre": "al.genre", "year": "al.year", "format": "t.format",
                "sample_rate": "t.sample_rate", "bit_depth": "t.bit_depth",
                "source": "t.source", "composer": "t.composer",
            }
            col = col_map.get(field)
            if not col:
                continue

            if op == "contains":
                conditions.append(f"{col} LIKE ?")
                params.append(f"%{value}%")
            elif op == "equals":
                conditions.append(f"{col} = ?")
                params.append(value)
            elif op == "not_equals":
                conditions.append(f"{col} != ?")
                params.append(value)
            elif op == "greater_than":
                conditions.append(f"{col} > ?")
                params.append(value)
            elif op == "less_than":
                conditions.append(f"{col} < ?")
                params.append(value)
            elif op == "starts_with":
                conditions.append(f"{col} LIKE ?")
                params.append(f"{value}%")
            elif op == "branch_of":
                # Walk the user's genre tree: match the value itself + all
                # its direct children. Only meaningful for the genre column
                # but we accept it on any text col (silently no-ops if the
                # value has no entries in the tree).
                from tune_server.library.genre_tree import expand_branch
                branch = expand_branch(str(value))
                if branch:
                    placeholders = ",".join("?" for _ in branch)
                    conditions.append(f"{col} IN ({placeholders})")
                    params.extend(sorted(branch))

        where = ""
        if conditions:
            joiner = " AND " if match_mode == "all" else " OR "
            where = "WHERE " + joiner.join(conditions)

        sort_col_map = {
            "title": "t.title", "artist": "ar.name", "album": "al.title",
            "year": "al.year", "duration": "t.duration_ms",
            "track_number": "t.track_number", "random": "RANDOM()",
        }
        order_col = sort_col_map.get(sort_by, "t.title")
        order_dir = "DESC" if sort_order == "desc" else "ASC"
        order = f"ORDER BY {order_col} {order_dir}"

        params.append(max_tracks)

        rows = await self._db.fetchall(
            f"""SELECT t.*, al.title as album_title, ar.name as artist_name,
                       al.cover_path as cover_path
                FROM tracks t
                LEFT JOIN albums al ON t.album_id = al.id
                LEFT JOIN artists ar ON t.artist_id = ar.id
                {where} {order} LIMIT ?""",
            tuple(params),
        )
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


async def seed_default_smart_playlists(repo: SmartPlaylistRepo) -> int:
    """Seed default smart playlists on first server start. Idempotent —
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


# ---------------------------------------------------------------------------
# full_text_search — raw SQL (used by routes, delegates to legacy repos)
# ---------------------------------------------------------------------------

async def full_text_search(db: Database, query: str, limit: int = 50) -> SearchResult:
    """Full-text search across artists, albums, tracks.

    Uses the SA repos (via short-name aliases) when available, falling back
    to raw SQL for any FTS5-specific queries.
    """
    from tune_server.db.sa_repository import AlbumRepo, ArtistRepo, TrackRepo

    track_repo = TrackRepo(db)
    album_repo = AlbumRepo(db)
    artist_repo = ArtistRepo(db)

    tracks = await track_repo.search(query, limit)
    albums = await album_repo.search(query, limit)
    artists = await artist_repo.search(query, limit)

    # Enrich: also fetch albums/tracks for matching artists.
    # Limit to 5 artists to avoid N+1 explosion on broad queries
    # (each artist triggers 2 extra queries for albums + tracks).
    seen_album_ids = {a.id for a in albums if a.id}
    seen_track_ids = {t.id for t in tracks if t.id}
    for artist in artists[:5]:
        if not artist.id:
            continue
        artist_albums = await album_repo.list_by_artist(artist.id)
        for al in artist_albums:
            if al.id and al.id not in seen_album_ids:
                albums.append(al)
                seen_album_ids.add(al.id)
        artist_tracks = await track_repo.list_by_artist(artist.id)
        for tr in artist_tracks:
            if tr.id and tr.id not in seen_track_ids:
                tracks.append(tr)
                seen_track_ids.add(tr.id)
        if len(albums) >= limit and len(tracks) >= limit:
            break
    tracks = tracks[:limit]
    albums = albums[:limit]

    return SearchResult(tracks=tracks, albums=albums, artists=artists)

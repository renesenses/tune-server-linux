from __future__ import annotations

from typing import Optional

import structlog

from tune_server.db.engine import Database
from tune_server.models import Album, Artist, Playlist, RadioStation, RadioStationCreate, SearchResult, Track, TrackCredit

logger = structlog.get_logger()


def _row_get(row, key, default=None):
    # sqlite3.Row supports indexed access but not .get(); SA Row supports both.
    keys = row.keys() if hasattr(row, "keys") else []
    return row[key] if key in keys else default


def _row_to_artist(row) -> Artist:
    return Artist(
        id=row["id"],
        name=row["name"],
        sort_name=_row_get(row, "sort_name"),
        musicbrainz_id=_row_get(row, "musicbrainz_id"),
        discogs_id=_row_get(row, "discogs_id"),
        bio=_row_get(row, "bio"),
        image_path=_row_get(row, "image_path"),
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
    )


class ArtistRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, artist_id: int) -> Optional[Artist]:
        row = await self._db.fetchone("SELECT * FROM artists WHERE id = ?", (artist_id,))
        return _row_to_artist(row) if row else None

    async def get_by_name(self, name: str) -> Optional[Artist]:
        row = await self._db.fetchone("SELECT * FROM artists WHERE name = ?", (name,))
        return _row_to_artist(row) if row else None

    _PRINCIPAL_ONLY = (
        "(EXISTS (SELECT 1 FROM albums WHERE artist_id = artists.id) "
        "OR EXISTS (SELECT 1 FROM tracks WHERE artist_id = artists.id))"
    )

    async def list(self, limit: int = 100, offset: int = 0, principal_only: bool = False) -> list[Artist]:
        where = f"WHERE {self._PRINCIPAL_ONLY}" if principal_only else ""
        rows = await self._db.fetchall(
            f"SELECT * FROM artists {where} ORDER BY sort_name, name LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [_row_to_artist(r) for r in rows]

    async def count(self, principal_only: bool = False) -> int:
        where = f"WHERE {self._PRINCIPAL_ONLY}" if principal_only else ""
        row = await self._db.fetchone(f"SELECT COUNT(*) as cnt FROM artists {where}")
        return row["cnt"]

    async def list_initial_letters(self, principal_only: bool = False) -> list[tuple[str, int]]:
        where = f"WHERE {self._PRINCIPAL_ONLY}" if principal_only else ""
        rows = await self._db.fetchall(
            f"""SELECT
                 CASE WHEN UPPER(SUBSTR(COALESCE(sort_name, name), 1, 1)) BETWEEN 'A' AND 'Z'
                      THEN UPPER(SUBSTR(COALESCE(sort_name, name), 1, 1)) ELSE '#' END AS letter,
                 COUNT(*) AS cnt
               FROM artists {where} GROUP BY letter ORDER BY letter""",
        )
        return [(r["letter"], r["cnt"]) for r in rows]

    async def list_by_letter(self, letter: str, limit: int = 500, offset: int = 0, principal_only: bool = False) -> list[Artist]:
        clauses = []
        params: list = []
        if letter == "#":
            clauses.append("UPPER(SUBSTR(COALESCE(sort_name, name), 1, 1)) NOT BETWEEN 'A' AND 'Z'")
        else:
            clauses.append("UPPER(SUBSTR(COALESCE(sort_name, name), 1, 1)) = ?")
            params.append(letter.upper())
        if principal_only:
            clauses.append(self._PRINCIPAL_ONLY)
        where = " WHERE " + " AND ".join(clauses)
        params.extend([limit, offset])
        rows = await self._db.fetchall(
            f"SELECT * FROM artists{where} ORDER BY sort_name, name LIMIT ? OFFSET ?",
            tuple(params),
        )
        return [_row_to_artist(r) for r in rows]

    async def create(self, artist: Artist) -> int:
        result = await self._db.execute(
            """INSERT INTO artists (name, sort_name, musicbrainz_id, discogs_id, bio, image_path)
               VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
            (artist.name, artist.sort_name, artist.musicbrainz_id,
             artist.discogs_id, artist.bio, artist.image_path),
        )
        await self._db.commit()
        return result.lastrowid

    async def get_or_create(self, name: str) -> Artist:
        existing = await self.get_by_name(name)
        if existing:
            return existing
        artist_id = await self.create(Artist(name=name, sort_name=name))
        return Artist(id=artist_id, name=name, sort_name=name)

    async def update(self, artist: Artist) -> None:
        await self._db.execute(
            """UPDATE artists SET name=?, sort_name=?, musicbrainz_id=?,
               discogs_id=?, bio=?, image_path=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (artist.name, artist.sort_name, artist.musicbrainz_id,
             artist.discogs_id, artist.bio, artist.image_path, artist.id),
        )
        await self._db.commit()

    async def delete(self, artist_id: int) -> None:
        await self._db.execute("DELETE FROM artists WHERE id = ?", (artist_id,))
        await self._db.commit()

    async def count_without_image(self) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM artists WHERE image_path IS NULL OR image_path = ''"
        )
        return row["cnt"]

    async def search(self, query: str, limit: int = 50) -> list[Artist]:
        if getattr(self._db, 'engine_name', 'sqlite') == 'postgres':
            rows = await self._db.fetchall(
                """SELECT a.* FROM artists a
                   WHERE a.fts_vector @@ plainto_tsquery('simple', ?)
                   ORDER BY ts_rank(a.fts_vector, plainto_tsquery('simple', ?)) DESC
                   LIMIT ?""",
                (query, query, limit),
            )
        else:
            rows = await self._db.fetchall(
                """SELECT a.* FROM artists a
                   JOIN artists_fts fts ON a.id = fts.rowid
                   WHERE artists_fts MATCH ? LIMIT ?""",
                (query + "*", limit),
            )
        return [_row_to_artist(r) for r in rows]


class AlbumRepo:
    # ar.name aliased to joined_artist_name to avoid clash with al.artist_name
    # (denormalized column added by migration). _row_to_album prefers the join.
    _SELECT = """SELECT al.*, ar.name as joined_artist_name,
               al.sample_rate as max_sample_rate, al.bit_depth as max_bit_depth,
               al.format as dominant_format
               FROM albums al
               LEFT JOIN artists ar ON al.artist_id = ar.id"""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, album_id: int) -> Optional[Album]:
        row = await self._db.fetchone(
            f"{self._SELECT} WHERE al.id = ?",
            (album_id,),
        )
        return _row_to_album(row) if row else None

    async def get_by_title_and_artist(self, title: str, artist_id: int) -> Optional[Album]:
        row = await self._db.fetchone(
            f"{self._SELECT} WHERE al.title = ? AND al.artist_id = ?",
            (title, artist_id),
        )
        return _row_to_album(row) if row else None

    async def get_by_title(self, title: str) -> Album | None:
        row = await self._db.fetchone(
            f"{self._SELECT} WHERE al.title = ? LIMIT 1",
            (title,),
        )
        return _row_to_album(row) if row else None

    async def list(self, limit: int = 100, offset: int = 0, quality: str | None = None,
                   format: str | None = None, sample_rate: int | None = None) -> list[Album]:
        where_clauses = []
        params: list = []
        if format:
            where_clauses.append("tq.dominant_format = ?")
            params.append(format.lower())
        if sample_rate:
            where_clauses.append("tq.max_sample_rate >= ?")
            params.append(sample_rate)
        where = ""
        if where_clauses:
            where = " WHERE " + " AND ".join(where_clauses)
        params.extend([limit, offset])
        rows = await self._db.fetchall(
            f"{self._SELECT}{where} ORDER BY al.title LIMIT ? OFFSET ?",
            tuple(params),
        )
        albums = [_row_to_album(r) for r in rows]
        if quality:
            albums = [a for a in albums if a.quality == quality]
        return albums

    async def list_recent(self, limit: int = 50) -> list[Album]:
        rows = await self._db.fetchall(
            f"{self._SELECT} ORDER BY al.created_at DESC LIMIT ?",
            (limit,),
        )
        return [_row_to_album(r) for r in rows]

    async def list_by_artist(self, artist_id: int) -> list[Album]:
        rows = await self._db.fetchall(
            f"""{self._SELECT}
               WHERE al.artist_id = ?
                  OR al.id IN (SELECT DISTINCT album_id FROM tracks WHERE artist_id = ?)
               ORDER BY al.year""",
            (artist_id, artist_id),
        )
        return [_row_to_album(r) for r in rows]

    async def count(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) as cnt FROM albums")
        return row["cnt"]

    async def list_initial_letters(self) -> list[tuple[str, int]]:
        """Return (letter, count) for alphabetical navigation. Non-alpha grouped as '#'."""
        rows = await self._db.fetchall(
            """SELECT
                 CASE WHEN UPPER(SUBSTR(title, 1, 1)) BETWEEN 'A' AND 'Z'
                      THEN UPPER(SUBSTR(title, 1, 1)) ELSE '#' END AS letter,
                 COUNT(*) AS cnt
               FROM albums GROUP BY letter ORDER BY letter""",
        )
        return [(r["letter"], r["cnt"]) for r in rows]

    async def list_by_letter(self, letter: str, limit: int = 500, offset: int = 0) -> list[Album]:
        if letter == "#":
            where = "UPPER(SUBSTR(al.title, 1, 1)) NOT BETWEEN 'A' AND 'Z'"
            params: tuple = (limit, offset)
        else:
            where = "UPPER(SUBSTR(al.title, 1, 1)) = ?"
            params = (letter.upper(), limit, offset)
        rows = await self._db.fetchall(
            f"""SELECT al.*, ar.name as artist_name
                FROM albums al LEFT JOIN artists ar ON al.artist_id = ar.id
                WHERE {where} ORDER BY al.title LIMIT ? OFFSET ?""",
            params,
        )
        return [_row_to_album(r) for r in rows]

    async def count_by_letter(self, letter: str) -> int:
        if letter == "#":
            row = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM albums WHERE UPPER(SUBSTR(title, 1, 1)) NOT BETWEEN 'A' AND 'Z'",
            )
        else:
            row = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM albums WHERE UPPER(SUBSTR(title, 1, 1)) = ?",
                (letter.upper(),),
            )
        return row["cnt"]

    async def create(self, album: Album) -> int:
        result = await self._db.execute(
            """INSERT INTO albums (title, artist_id, year, genre, disc_count,
               track_count, cover_path, source, source_id, label, catalog_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (album.title, album.artist_id, album.year, album.genre,
             album.disc_count, album.track_count, album.cover_path,
             album.source, album.source_id, album.label, album.catalog_number),
        )
        await self._db.commit()
        return result.lastrowid

    async def update_label(self, album_id: int, label: str | None,
                            catalog_number: str | None = None) -> None:
        """Backfill label / catalog_number for an album that doesn't have one yet.

        Used by the scanner: a scan-time tag may carry the label even if it
        was missing on the very first track that created the album.
        """
        await self._db.execute(
            """UPDATE albums
                 SET label = COALESCE(NULLIF(label, ''), ?),
                     catalog_number = COALESCE(NULLIF(catalog_number, ''), ?),
                     updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (label, catalog_number, album_id),
        )
        await self._db.commit()

    async def get_or_create(self, title: str, artist_id: int, **kwargs) -> Album:
        existing = await self.get_by_title_and_artist(title, artist_id)
        if existing:
            return existing
        album = Album(title=title, artist_id=artist_id, **kwargs)
        album_id = await self.create(album)
        album.id = album_id
        return album

    async def update(self, album: Album) -> None:
        await self._db.execute(
            """UPDATE albums SET title=?, artist_id=?, year=?, genre=?, disc_count=?,
               track_count=?, cover_path=?, source=?, source_id=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (album.title, album.artist_id, album.year, album.genre,
             album.disc_count, album.track_count, album.cover_path,
             album.source, album.source_id, album.id),
        )
        await self._db.commit()

    async def refresh_quality(self, album_id: int) -> None:
        """Recompute and store format/sample_rate/bit_depth from tracks."""
        row = await self._db.fetchone(
            """SELECT MAX(sample_rate) as sr, MAX(bit_depth) as bd,
                      (SELECT format FROM tracks WHERE album_id = ? AND format IS NOT NULL
                       GROUP BY format ORDER BY COUNT(*) DESC LIMIT 1) as fmt
               FROM tracks WHERE album_id = ?""",
            (album_id, album_id),
        )
        if row:
            await self._db.execute(
                "UPDATE albums SET sample_rate=?, bit_depth=?, format=? WHERE id=?",
                (row["sr"], row["bd"], row["fmt"], album_id),
            )
            await self._db.commit()

    async def get_dominant_sample_rate(self, album_id: int) -> int | None:
        """Return the most common sample_rate among an album's tracks, or None if empty."""
        row = await self._db.fetchone(
            """SELECT sample_rate FROM tracks
               WHERE album_id = ? AND sample_rate IS NOT NULL
               GROUP BY sample_rate ORDER BY COUNT(*) DESC LIMIT 1""",
            (album_id,),
        )
        return row["sample_rate"] if row else None

    async def update_track_count(self, album_id: int) -> None:
        await self._db.execute(
            """UPDATE albums SET track_count = (
                SELECT COUNT(*) FROM tracks WHERE album_id = ?
            ) WHERE id = ?""",
            (album_id, album_id),
        )
        await self._db.commit()

    async def delete(self, album_id: int) -> None:
        await self._db.execute("DELETE FROM albums WHERE id = ?", (album_id,))
        await self._db.commit()

    async def delete_orphans(self) -> int:
        """Delete albums that have no tracks."""
        cursor = await self._db.execute(
            """DELETE FROM albums WHERE id NOT IN (
                SELECT DISTINCT album_id FROM tracks WHERE album_id IS NOT NULL
            )""",
        )
        await self._db.commit()
        return cursor.rowcount

    async def merge_duplicates(self) -> int:
        """Merge albums with the same title: reassign tracks, delete dupes."""
        if getattr(self._db, 'engine_name', 'sqlite') == 'postgres':
            agg = "STRING_AGG(id::text, ',')"
        else:
            agg = "GROUP_CONCAT(id)"
        rows = await self._db.fetchall(
            f"""SELECT title, MIN(id) as keep_id, {agg} as all_ids
               FROM albums GROUP BY title HAVING COUNT(*) > 1""",
        )
        merged = 0
        for row in rows:
            keep_id = row["keep_id"]
            all_ids = [int(x) for x in row["all_ids"].split(",")]
            delete_ids = [x for x in all_ids if x != keep_id]
            for did in delete_ids:
                await self._db.execute(
                    "UPDATE tracks SET album_id = ? WHERE album_id = ?",
                    (keep_id, did),
                )
                await self._db.execute("DELETE FROM albums WHERE id = ?", (did,))
                merged += 1
            await self._db.execute(
                """UPDATE albums SET track_count = (
                    SELECT COUNT(*) FROM tracks WHERE album_id = ?
                ) WHERE id = ?""",
                (keep_id, keep_id),
            )
        await self._db.commit()
        return merged

    async def update_bio(self, album_id: int, bio: str) -> None:
        await self._db.execute(
            "UPDATE albums SET bio = ? WHERE id = ?", (bio, album_id)
        )
        await self._db.commit()

    async def count_without_cover(self) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM albums WHERE cover_path IS NULL"
        )
        return row["cnt"]

    async def count_without_genre(self) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM albums WHERE genre IS NULL OR genre = ''"
        )
        return row["cnt"]

    async def count_without_year(self) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM albums WHERE year IS NULL OR year = 0"
        )
        return row["cnt"]

    async def list_without_cover(self) -> list[Album]:
        rows = await self._db.fetchall(
            """SELECT al.*, ar.name as artist_name
               FROM albums al LEFT JOIN artists ar ON al.artist_id = ar.id
               WHERE al.cover_path IS NULL ORDER BY al.title""",
        )
        return [_row_to_album(r) for r in rows]

    async def search(self, query: str, limit: int = 50) -> list[Album]:
        like_pat = f"%{query}%"
        if getattr(self._db, 'engine_name', 'sqlite') == 'postgres':
            rows = await self._db.fetchall(
                """SELECT DISTINCT al.*, ar.name as artist_name FROM albums al
                   LEFT JOIN artists ar ON al.artist_id = ar.id
                   WHERE al.fts_vector @@ plainto_tsquery('simple', ?)
                      OR ar.name ILIKE ?
                      OR al.artist_name ILIKE ?
                      OR al.genre ILIKE ?
                      OR CAST(al.year AS TEXT) = ?
                      OR al.label ILIKE ?
                   LIMIT ?""",
                (query, like_pat, like_pat, like_pat, query.strip(), like_pat, limit),
            )
        else:
            rows = await self._db.fetchall(
                """SELECT DISTINCT al.*, ar.name as artist_name FROM albums al
                   LEFT JOIN artists ar ON al.artist_id = ar.id
                   LEFT JOIN albums_fts fts ON al.id = fts.rowid AND albums_fts MATCH ?
                   WHERE fts.rowid IS NOT NULL
                      OR ar.name LIKE ?
                      OR al.artist_name LIKE ?
                      OR al.genre LIKE ?
                      OR CAST(al.year AS TEXT) = ?
                      OR al.label LIKE ?
                   LIMIT ?""",
                (query + "*", like_pat, like_pat, like_pat, query.strip(), like_pat, limit),
            )
        return [_row_to_album(r) for r in rows]


class TrackRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    _SELECT = """SELECT t.*, al.title as album_title, ar.name as artist_name,
                        al.cover_path as cover_path
                 FROM tracks t
                 LEFT JOIN albums al ON t.album_id = al.id
                 LEFT JOIN artists ar ON t.artist_id = ar.id"""

    async def get(self, track_id: int) -> Optional[Track]:
        row = await self._db.fetchone(f"{self._SELECT} WHERE t.id = ?", (track_id,))
        return _row_to_track(row) if row else None

    async def get_by_path(self, file_path: str) -> Optional[Track]:
        row = await self._db.fetchone(f"{self._SELECT} WHERE t.file_path = ?", (file_path,))
        return _row_to_track(row) if row else None

    async def get_by_source(self, source: str, source_id: str) -> Optional[Track]:
        row = await self._db.fetchone(
            f"{self._SELECT} WHERE t.source = ? AND t.source_id = ?",
            (source, source_id),
        )
        return _row_to_track(row) if row else None

    async def get_by_sources(self, items: list[tuple[str, str]]) -> dict[tuple[str, str], Track]:
        """Batch lookup tracks by (source, source_id) pairs."""
        if not items:
            return {}
        conditions = " OR ".join(["(t.source = ? AND t.source_id = ?)"] * len(items))
        params: list = []
        for source, source_id in items:
            params.extend([source, source_id])
        rows = await self._db.fetchall(
            f"{self._SELECT} WHERE {conditions}",
            tuple(params),
        )
        result: dict[tuple[str, str], Track] = {}
        for r in rows:
            t = _row_to_track(r)
            result[(t.source, t.source_id)] = t
        return result

    async def list(self, limit: int = 100, offset: int = 0) -> list[Track]:
        rows = await self._db.fetchall(
            f"{self._SELECT} ORDER BY t.title LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [_row_to_track(r) for r in rows]

    async def list_by_album(self, album_id: int) -> list[Track]:
        rows = await self._db.fetchall(
            f"{self._SELECT} WHERE t.album_id = ? ORDER BY t.disc_number, t.track_number, t.file_path",
            (album_id,),
        )
        return [_row_to_track(r) for r in rows]

    async def list_by_artist(self, artist_id: int) -> list[Track]:
        rows = await self._db.fetchall(
            f"{self._SELECT} WHERE t.artist_id = ? ORDER BY t.title",
            (artist_id,),
        )
        return [_row_to_track(r) for r in rows]

    async def count(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) as cnt FROM tracks")
        return row["cnt"]

    async def list_initial_letters(self) -> list[tuple[str, int]]:
        rows = await self._db.fetchall(
            """SELECT
                 CASE WHEN UPPER(SUBSTR(title, 1, 1)) BETWEEN 'A' AND 'Z'
                      THEN UPPER(SUBSTR(title, 1, 1)) ELSE '#' END AS letter,
                 COUNT(*) AS cnt
               FROM tracks GROUP BY letter ORDER BY letter""",
        )
        return [(r["letter"], r["cnt"]) for r in rows]

    async def list_by_letter(self, letter: str, limit: int = 500, offset: int = 0) -> list[Track]:
        if letter == "#":
            where = "UPPER(SUBSTR(t.title, 1, 1)) NOT BETWEEN 'A' AND 'Z'"
            params: tuple = (limit, offset)
        else:
            where = "UPPER(SUBSTR(t.title, 1, 1)) = ?"
            params = (letter.upper(), limit, offset)
        rows = await self._db.fetchall(
            f"{self._SELECT} WHERE {where} ORDER BY t.title LIMIT ? OFFSET ?",
            params,
        )
        return [_row_to_track(r) for r in rows]

    async def count_by_letter(self, letter: str) -> int:
        if letter == "#":
            row = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM tracks WHERE UPPER(SUBSTR(title, 1, 1)) NOT BETWEEN 'A' AND 'Z'",
            )
        else:
            row = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM tracks WHERE UPPER(SUBSTR(title, 1, 1)) = ?",
                (letter.upper(),),
            )
        return row["cnt"]

    async def create(self, track: Track) -> int:
        result = await self._db.execute(
            """INSERT INTO tracks (title, album_id, artist_id, disc_number,
               track_number, duration_ms, file_path, format, sample_rate,
               bit_depth, channels, source, source_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (track.title, track.album_id, track.artist_id, track.disc_number,
             track.track_number, track.duration_ms, track.file_path,
             track.format, track.sample_rate, track.bit_depth,
             track.channels, track.source, track.source_id),
        )
        await self._db.commit()
        return result.lastrowid

    async def update(self, track: Track) -> None:
        await self._db.execute(
            """UPDATE tracks SET title=?, album_id=?, artist_id=?, disc_number=?,
               track_number=?, duration_ms=?, file_path=?, format=?, sample_rate=?,
               bit_depth=?, channels=?, source=?, source_id=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (track.title, track.album_id, track.artist_id, track.disc_number,
             track.track_number, track.duration_ms, track.file_path,
             track.format, track.sample_rate, track.bit_depth,
             track.channels, track.source, track.source_id, track.id),
        )
        await self._db.commit()

    async def delete(self, track_id: int) -> None:
        await self._db.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        await self._db.commit()

    async def delete_by_path(self, file_path: str) -> None:
        await self._db.execute("DELETE FROM tracks WHERE file_path = ?", (file_path,))
        await self._db.commit()

    async def deduplicate(self) -> int:
        """Remove duplicate tracks (same audio_hash), keeping the lowest id."""
        cursor = await self._db.execute(
            """DELETE FROM tracks WHERE id NOT IN (
                SELECT MIN(id) FROM tracks
                WHERE album_id IS NOT NULL AND audio_hash IS NOT NULL
                GROUP BY audio_hash
            ) AND id IN (
                SELECT t.id FROM tracks t
                JOIN (
                    SELECT audio_hash
                    FROM tracks WHERE album_id IS NOT NULL AND audio_hash IS NOT NULL
                    GROUP BY audio_hash
                    HAVING COUNT(*) > 1
                ) d ON t.audio_hash = d.audio_hash
            )""",
        )
        await self._db.commit()
        return cursor.rowcount

    async def find_by_audio_hash(self, audio_hash: str) -> Optional[Track]:
        row = await self._db.fetchone(
            f"{self._SELECT} WHERE t.audio_hash = ? LIMIT 1", (audio_hash,)
        )
        return _row_to_track(row) if row else None

    async def get_mtime(self, file_path: str) -> Optional[float]:
        row = await self._db.fetchone(
            "SELECT file_mtime FROM tracks WHERE file_path = ?", (file_path,)
        )
        return row["file_mtime"] if row else None

    async def update_mtime(self, file_path: str, mtime: float) -> None:
        await self._db.execute(
            "UPDATE tracks SET file_mtime = ? WHERE file_path = ?", (mtime, file_path)
        )
        await self._db.commit()

    async def update_audio_hash(self, file_path: str, audio_hash: str) -> None:
        await self._db.execute(
            "UPDATE tracks SET audio_hash = ? WHERE file_path = ?", (audio_hash, file_path)
        )
        await self._db.commit()

    async def get_all_paths(self) -> set[str]:
        rows = await self._db.fetchall(
            "SELECT file_path FROM tracks WHERE source = 'local'"
        )
        return {r["file_path"] for r in rows}

    async def search(self, query: str, limit: int = 50) -> list[Track]:
        like_pat = f"%{query}%"
        if getattr(self._db, 'engine_name', 'sqlite') == 'postgres':
            rows = await self._db.fetchall(
                """SELECT DISTINCT t.*, al.title as album_title, ar.name as artist_name
                    FROM tracks t
                    LEFT JOIN albums al ON t.album_id = al.id
                    LEFT JOIN artists ar ON t.artist_id = ar.id
                    LEFT JOIN track_credits tc ON tc.track_id = t.id
                    WHERE t.fts_vector @@ plainto_tsquery('simple', ?)
                       OR ar.name ILIKE ?
                       OR t.genre ILIKE ?
                       OR t.composer ILIKE ?
                       OR CAST(al.year AS TEXT) = ?
                       OR tc.artist_name ILIKE ?
                       OR tc.instrument ILIKE ?
                    LIMIT ?""",
                (query, like_pat, like_pat, like_pat, query.strip(), like_pat, like_pat, limit),
            )
        else:
            rows = await self._db.fetchall(
                """SELECT DISTINCT t.*, al.title as album_title, ar.name as artist_name
                    FROM tracks t
                    LEFT JOIN albums al ON t.album_id = al.id
                    LEFT JOIN artists ar ON t.artist_id = ar.id
                    LEFT JOIN track_credits tc ON tc.track_id = t.id
                    LEFT JOIN tracks_fts fts ON t.id = fts.rowid AND tracks_fts MATCH ?
                    WHERE fts.rowid IS NOT NULL
                       OR ar.name LIKE ?
                       OR t.genre LIKE ?
                       OR t.composer LIKE ?
                       OR CAST(al.year AS TEXT) = ?
                       OR tc.artist_name LIKE ?
                       OR tc.instrument LIKE ?
                    LIMIT ?""",
                (query + "*", like_pat, like_pat, like_pat, query.strip(), like_pat, like_pat, limit),
            )
        return [_row_to_track(r) for r in rows]

    async def get_multiple(self, track_ids: list[int]) -> list[Track]:
        if not track_ids:
            return []
        placeholders = ",".join("?" * len(track_ids))
        rows = await self._db.fetchall(
            f"""{self._SELECT} WHERE t.id IN ({placeholders})""",
            tuple(track_ids),
        )
        # Preserve caller's ordering (e.g. album track_number order)
        by_id = {r["id"]: r for r in rows}
        ordered = [by_id[tid] for tid in track_ids if tid in by_id]
        return [_row_to_track(r) for r in ordered]

    async def list_by_directory(self, directory: str) -> list[Track]:
        """Return tracks directly in a directory (not in subdirectories)."""
        prefix = directory.replace("\\", "/").rstrip("/") + "/"
        like_prefix = prefix + "%"
        like_nested = prefix + "%/%"
        rows = await self._db.fetchall(
            f"""{self._SELECT}
                WHERE t.file_path LIKE ?
                AND t.file_path NOT LIKE ?
                ORDER BY t.file_path""",
            (like_prefix, like_nested),
        )
        return [_row_to_track(r) for r in rows]

    async def list_subdirectories(self, directory: str) -> list[dict]:
        """Return immediate subdirectories with recursive track counts."""
        prefix = directory.replace("\\", "/").rstrip("/") + "/"
        like_prefix = prefix + "%"
        if getattr(self._db, 'engine_name', 'sqlite') == 'postgres':
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
                (len(prefix) + 1, like_prefix, len(prefix), len(prefix) + 1),
            )
        else:
            prefix_len = len(prefix) + 1  # SQL SUBSTR is 1-based
            rows = await self._db.fetchall(
                """SELECT
                    CASE
                        WHEN INSTR(SUBSTR(file_path, ?), '/') > 0
                        THEN SUBSTR(file_path, ?, INSTR(SUBSTR(file_path, ?), '/') - 1)
                        ELSE SUBSTR(file_path, ?)
                    END AS dir_name,
                    COUNT(*) AS track_count
                   FROM tracks
                   WHERE file_path LIKE ?
                   AND LENGTH(file_path) > ?
                   GROUP BY dir_name
                   HAVING INSTR(SUBSTR(file_path, ?), '/') > 0
                   ORDER BY dir_name""",
                (prefix_len, prefix_len, prefix_len, prefix_len,
                 like_prefix, len(prefix), prefix_len),
            )
        return [
            {"name": r["dir_name"], "path": prefix + r["dir_name"], "track_count": r["track_count"]}
            for r in rows
        ]

    async def count_without_artist(self) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM tracks WHERE artist_id IS NULL"
        )
        return row["cnt"]

    async def list_without_artist(self) -> list[Track]:
        rows = await self._db.fetchall(
            f"{self._SELECT} WHERE t.artist_id IS NULL ORDER BY t.title",
        )
        return [_row_to_track(r) for r in rows]

    async def update_waveform(self, track_id: int, waveform_data: str) -> None:
        await self._db.execute(
            "UPDATE tracks SET waveform_data = ?, waveform_generated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (waveform_data, track_id),
        )
        await self._db.commit()

    async def update_bpm(self, track_id: int, bpm: float) -> None:
        await self._db.execute(
            "UPDATE tracks SET bpm = ? WHERE id = ?",
            (bpm, track_id),
        )
        await self._db.commit()

    async def update_loudness(self, track_id: int, lufs: float) -> None:
        await self._db.execute(
            "UPDATE tracks SET loudness_lufs = ? WHERE id = ?",
            (lufs, track_id),
        )
        await self._db.commit()

    async def count_by_root(self, root_dir: str) -> int:
        """Count all tracks under a root directory."""
        prefix = root_dir.replace("\\", "/").rstrip("/") + "/"
        row = await self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM tracks WHERE file_path LIKE ?",
            (prefix + "%",),
        )
        return row["cnt"]


class PlayQueueRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_queue(self, zone_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            """SELECT pq.*, t.title, t.file_path, t.duration_ms, t.format,
                      t.sample_rate, t.bit_depth, t.channels,
                      al.title as album_title, ar.name as artist_name
               FROM play_queue pq
               JOIN tracks t ON pq.track_id = t.id
               LEFT JOIN albums al ON t.album_id = al.id
               LEFT JOIN artists ar ON t.artist_id = ar.id
               WHERE pq.zone_id = ? ORDER BY pq.position""",
            (zone_id,),
        )
        return [dict(r) for r in rows]

    async def get_current(self, zone_id: int) -> Optional[dict]:
        row = await self._db.fetchone(
            """SELECT pq.*, t.title, t.file_path, t.duration_ms, t.format,
                      t.sample_rate, t.bit_depth, t.channels, t.source, t.source_id,
                      al.title as album_title, ar.name as artist_name
               FROM play_queue pq
               JOIN tracks t ON pq.track_id = t.id
               LEFT JOIN albums al ON t.album_id = al.id
               LEFT JOIN artists ar ON t.artist_id = ar.id
               WHERE pq.zone_id = ? AND pq.is_current = 1""",
            (zone_id,),
        )
        return dict(row) if row else None

    async def set_queue(self, zone_id: int, track_ids: list[int]) -> None:
        await self._db.execute("DELETE FROM play_queue WHERE zone_id = ?", (zone_id,))
        if track_ids:
            params = [(zone_id, track_id, i, 1 if i == 0 else 0) for i, track_id in enumerate(track_ids)]
            await self._db.executemany(
                "INSERT INTO play_queue (zone_id, track_id, position, is_current) VALUES (?, ?, ?, ?)",
                params,
            )
        await self._db.commit()

    async def add_tracks(self, zone_id: int, track_ids: list[int], position: Optional[int] = None) -> None:
        if position is not None:
            await self._db.execute(
                "UPDATE play_queue SET position = position + ? WHERE zone_id = ? AND position >= ?",
                (len(track_ids), zone_id, position),
            )
        else:
            row = await self._db.fetchone(
                "SELECT COALESCE(MAX(position), -1) + 1 as next_pos FROM play_queue WHERE zone_id = ?",
                (zone_id,),
            )
            position = row["next_pos"]

        if track_ids:
            params = [(zone_id, track_id, position + i) for i, track_id in enumerate(track_ids)]
            await self._db.executemany(
                "INSERT INTO play_queue (zone_id, track_id, position) VALUES (?, ?, ?)",
                params,
            )
        await self._db.commit()

    async def set_current(self, zone_id: int, position: int) -> None:
        await self._db.execute(
            "UPDATE play_queue SET is_current = 0 WHERE zone_id = ?", (zone_id,)
        )
        await self._db.execute(
            "UPDATE play_queue SET is_current = 1 WHERE zone_id = ? AND position = ?",
            (zone_id, position),
        )
        await self._db.commit()

    async def clear(self, zone_id: int) -> None:
        await self._db.execute("DELETE FROM play_queue WHERE zone_id = ?", (zone_id,))
        await self._db.commit()

    async def count(self, zone_id: int) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM play_queue WHERE zone_id = ?", (zone_id,)
        )
        return row["cnt"]


class ZoneRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, zone_id: int) -> Optional[dict]:
        row = await self._db.fetchone("SELECT * FROM zones WHERE id = ?", (zone_id,))
        return dict(row) if row else None

    async def list(self) -> list[dict]:
        rows = await self._db.fetchall("SELECT * FROM zones ORDER BY name")
        return [dict(r) for r in rows]

    async def create(self, name: str, output_type: str, output_device_id: str = None,
                     stereo_pair_id: str = None, stereo_channel: str = None) -> int:
        result = await self._db.execute(
            """INSERT INTO zones (name, output_type, output_device_id, stereo_pair_id, stereo_channel)
               VALUES (?, ?, ?, ?, ?) RETURNING id""",
            (name, output_type, output_device_id, stereo_pair_id, stereo_channel),
        )
        await self._db.commit()
        return result.lastrowid

    async def update(self, zone_id: int, **kwargs) -> None:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [zone_id]
        await self._db.execute(f"UPDATE zones SET {sets} WHERE id = ?", tuple(values))
        await self._db.commit()

    async def delete(self, zone_id: int) -> None:
        await self._db.execute("DELETE FROM play_queue WHERE zone_id = ?", (zone_id,))
        await self._db.execute("DELETE FROM zones WHERE id = ?", (zone_id,))
        await self._db.commit()


def _row_to_playlist(row) -> Playlist:
    return Playlist(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        track_count=row["track_count"] if "track_count" in row.keys() else 0,
    )


class PlaylistRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, name: str, description: Optional[str] = None) -> int:
        result = await self._db.execute(
            "INSERT INTO playlists (name, description) VALUES (?, ?) RETURNING id",
            (name, description),
        )
        await self._db.commit()
        return result.lastrowid

    async def get(self, playlist_id: int) -> Optional[Playlist]:
        row = await self._db.fetchone(
            """SELECT p.*, COALESCE(cnt.track_count, 0) as track_count
               FROM playlists p
               LEFT JOIN (
                   SELECT playlist_id, COUNT(*) as track_count
                   FROM playlist_tracks GROUP BY playlist_id
               ) cnt ON p.id = cnt.playlist_id
               WHERE p.id = ?""",
            (playlist_id,),
        )
        return _row_to_playlist(row) if row else None

    async def list(self, limit: int = 100, offset: int = 0) -> list[Playlist]:
        rows = await self._db.fetchall(
            """SELECT p.*, COALESCE(cnt.track_count, 0) as track_count
               FROM playlists p
               LEFT JOIN (
                   SELECT playlist_id, COUNT(*) as track_count
                   FROM playlist_tracks GROUP BY playlist_id
               ) cnt ON p.id = cnt.playlist_id
               ORDER BY p.name LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        return [_row_to_playlist(r) for r in rows]

    async def update(self, playlist_id: int, name: Optional[str] = None, description: Optional[str] = None) -> None:
        fields = {}
        if name is not None:
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [playlist_id]
        await self._db.execute(
            f"UPDATE playlists SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
            tuple(values),
        )
        await self._db.commit()

    async def delete(self, playlist_id: int) -> None:
        await self._db.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        await self._db.commit()

    async def get_tracks(self, playlist_id: int) -> list[Track]:
        rows = await self._db.fetchall(
            """SELECT t.*, al.title as album_title, ar.name as artist_name
               FROM playlist_tracks pt
               JOIN tracks t ON pt.track_id = t.id
               LEFT JOIN albums al ON t.album_id = al.id
               LEFT JOIN artists ar ON t.artist_id = ar.id
               WHERE pt.playlist_id = ?
               ORDER BY pt.position""",
            (playlist_id,),
        )
        return [_row_to_track(r) for r in rows]

    async def add_tracks(self, playlist_id: int, track_ids: list[int], position: Optional[int] = None) -> list[int]:
        """Add tracks to a playlist, skipping tracks already present.

        Returns the list of track_ids that were actually inserted (deduplicated).
        """
        if not track_ids:
            return []

        # Dedup against existing playlist tracks.
        existing_rows = await self._db.fetchall(
            "SELECT track_id FROM playlist_tracks WHERE playlist_id = ?",
            (playlist_id,),
        )
        existing = {row["track_id"] for row in existing_rows}
        # Preserve caller order and also dedup the incoming list itself.
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
            await self._db.execute(
                "UPDATE playlist_tracks SET position = position + ? WHERE playlist_id = ? AND position >= ?",
                (len(new_ids), playlist_id, position),
            )
        else:
            row = await self._db.fetchone(
                "SELECT COALESCE(MAX(position), -1) + 1 as next_pos FROM playlist_tracks WHERE playlist_id = ?",
                (playlist_id,),
            )
            position = row["next_pos"]

        params = [(playlist_id, track_id, position + i) for i, track_id in enumerate(new_ids)]
        await self._db.executemany(
            "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
            params,
        )
        await self._db.commit()
        return new_ids

    async def remove_track(self, playlist_id: int, track_id: int) -> None:
        row = await self._db.fetchone(
            "SELECT position FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
            (playlist_id, track_id),
        )
        if row:
            await self._db.execute(
                "DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
                (playlist_id, track_id),
            )
            await self._db.execute(
                "UPDATE playlist_tracks SET position = position - 1 WHERE playlist_id = ? AND position > ?",
                (playlist_id, row["position"]),
            )
            await self._db.commit()

    # Alias kept in sync with SAPlaylistRepo (which had two overlapping
    # methods named remove_track and was renamed in v0.7.56 to remove the
    # ambiguity). The route always calls remove_track_by_id; the SQLite
    # version only ever had the by-id signature.
    remove_track_by_id = remove_track

    async def reorder_tracks(self, playlist_id: int, track_ids: list[int]) -> None:
        await self._db.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id = ?",
            (playlist_id,),
        )
        if track_ids:
            params = [(playlist_id, track_id, i) for i, track_id in enumerate(track_ids)]
            await self._db.executemany(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                params,
            )
        await self._db.commit()


def _row_to_radio_station(row) -> RadioStation:
    return RadioStation(
        id=row["id"],
        name=row["name"],
        stream_url=row["stream_url"],
        logo_url=row["logo_url"],
        genre=row["genre"],
        tags=row["tags"],
        codec=row["codec"],
        country=row["country"],
        homepage_url=row["homepage_url"],
        favorite=bool(row["favorite"]),
    )


class RadioStationRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, station: RadioStationCreate) -> int:
        result = await self._db.execute(
            """INSERT INTO radio_stations (name, stream_url, logo_url, genre, tags, codec, country, homepage_url, favorite)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (station.name, station.stream_url, station.logo_url, station.genre,
             station.tags, station.codec, station.country, station.homepage_url,
             int(station.favorite)),
        )
        await self._db.commit()
        return result.lastrowid

    async def get(self, station_id: int) -> Optional[RadioStation]:
        row = await self._db.fetchone("SELECT * FROM radio_stations WHERE id = ?", (station_id,))
        return _row_to_radio_station(row) if row else None

    async def get_by_url(self, stream_url: str) -> Optional[RadioStation]:
        row = await self._db.fetchone("SELECT * FROM radio_stations WHERE stream_url = ?", (stream_url,))
        return _row_to_radio_station(row) if row else None

    async def list(
        self, limit: int = 100, offset: int = 0,
        genre: Optional[str] = None, favorite: Optional[bool] = None,
    ) -> list[RadioStation]:
        conditions = []
        params: list = []
        if genre:
            conditions.append("genre = ?")
            params.append(genre)
        if favorite is not None:
            conditions.append("favorite = ?")
            params.append(int(favorite))
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])
        rows = await self._db.fetchall(
            f"SELECT * FROM radio_stations{where} ORDER BY favorite DESC, name LIMIT ? OFFSET ?",
            tuple(params),
        )
        return [_row_to_radio_station(r) for r in rows]

    async def update(self, station_id: int, **kwargs) -> None:
        if "favorite" in kwargs and isinstance(kwargs["favorite"], bool):
            kwargs["favorite"] = int(kwargs["favorite"])
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [station_id]
        await self._db.execute(
            f"UPDATE radio_stations SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
            tuple(values),
        )
        await self._db.commit()

    async def delete(self, station_id: int) -> None:
        await self._db.execute("DELETE FROM radio_stations WHERE id = ?", (station_id,))
        await self._db.commit()

    async def count(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) as cnt FROM radio_stations")
        return row["cnt"]


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
        max_tracks = sp.get("max_tracks", 200)

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


class PartyVoteRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def increment(self, zone_id: int, position: int, title: str, artist: str | None) -> int:
        """Increment vote count. Create row if not exists. Return new count."""
        row = await self._db.fetchone(
            "SELECT id, vote_count FROM party_votes WHERE zone_id = ? AND queue_position = ?",
            (zone_id, position))
        if row:
            new_count = row["vote_count"] + 1
            await self._db.execute(
                "UPDATE party_votes SET vote_count = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_count, row["id"]))
            await self._db.commit()
            return new_count
        else:
            await self._db.execute(
                "INSERT INTO party_votes (zone_id, track_title, track_artist, queue_position, vote_count) VALUES (?, ?, ?, ?, 1)",
                (zone_id, title, artist, position))
            await self._db.commit()
            return 1

    async def get_votes(self, zone_id: int) -> dict[int, int]:
        """Get all votes for a zone. Returns {position: count}."""
        rows = await self._db.fetchall(
            "SELECT queue_position, vote_count FROM party_votes WHERE zone_id = ?", (zone_id,))
        return {r["queue_position"]: r["vote_count"] for r in rows}

    async def clear(self, zone_id: int) -> int:
        """Clear all votes for a zone."""
        result = await self._db.execute(
            "DELETE FROM party_votes WHERE zone_id = ?", (zone_id,))
        await self._db.commit()
        return result.rowcount if hasattr(result, 'rowcount') else 0

    async def swap_positions(self, zone_id: int, pos_a: int, pos_b: int) -> None:
        """Swap vote records for two queue positions."""
        row_a = await self._db.fetchone(
            "SELECT id, vote_count FROM party_votes WHERE zone_id = ? AND queue_position = ?",
            (zone_id, pos_a))
        row_b = await self._db.fetchone(
            "SELECT id, vote_count FROM party_votes WHERE zone_id = ? AND queue_position = ?",
            (zone_id, pos_b))
        if row_a and row_b:
            await self._db.execute(
                "UPDATE party_votes SET queue_position = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (pos_b, row_a["id"]))
            await self._db.execute(
                "UPDATE party_votes SET queue_position = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (pos_a, row_b["id"]))
        elif row_a:
            await self._db.execute(
                "UPDATE party_votes SET queue_position = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (pos_b, row_a["id"]))
        elif row_b:
            await self._db.execute(
                "UPDATE party_votes SET queue_position = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (pos_a, row_b["id"]))
        await self._db.commit()


class AlbumRatingRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def rate(self, album_id: int, rating: int, note: str | None = None, profile_id: int | None = None) -> dict:
        await self._db.execute(
            """INSERT INTO album_ratings (album_id, profile_id, rating, note)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(album_id, profile_id) DO UPDATE SET rating=?, note=?, updated_at=CURRENT_TIMESTAMP""",
            (album_id, profile_id, rating, note, rating, note))
        await self._db.commit()
        return {"album_id": album_id, "rating": rating, "note": note}

    async def get(self, album_id: int, profile_id: int | None = None) -> dict | None:
        row = await self._db.fetchone(
            "SELECT rating, note FROM album_ratings WHERE album_id = ? AND (profile_id = ? OR profile_id IS NULL)",
            (album_id, profile_id))
        if row:
            return {"album_id": album_id, "rating": row["rating"], "note": row["note"]}
        return None

    async def top_rated(self, limit: int = 20) -> list:
        rows = await self._db.fetchall(
            """SELECT ar.album_id, a.title, a.artist_name, a.cover_path, ar.rating, ar.note
               FROM album_ratings ar JOIN albums a ON ar.album_id = a.id
               ORDER BY ar.rating DESC, ar.updated_at DESC LIMIT ?""",
            (limit,))
        return [{"album_id": r["album_id"], "title": r["title"], "artist_name": r["artist_name"],
                 "cover_path": r["cover_path"], "rating": r["rating"], "note": r["note"]} for r in rows]


async def full_text_search(db: Database, query: str, limit: int = 50) -> SearchResult:
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


# ---------------------------------------------------------------------------
# RadioFavoriteRepo
# ---------------------------------------------------------------------------

class RadioFavoriteRepo:
    def __init__(self, db):
        self._db = db

    async def ensure_table(self) -> None:
        if getattr(self._db, 'engine_name', 'sqlite') == 'postgres':
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS radio_favorites (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL DEFAULT '',
                    station_name TEXT NOT NULL DEFAULT '',
                    cover_url TEXT,
                    stream_url TEXT,
                    saved_at TEXT NOT NULL DEFAULT (NOW()::text)
                )
            """)
        else:
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS radio_favorites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL DEFAULT '',
                    station_name TEXT NOT NULL DEFAULT '',
                    cover_url TEXT,
                    stream_url TEXT,
                    saved_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
        await self._db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_radio_favorites_dedup
            ON radio_favorites(title, artist)
        """)
        await self._db.commit()

    async def list(self, limit: int = 200, offset: int = 0) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM radio_favorites ORDER BY saved_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in rows]

    async def count(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) as cnt FROM radio_favorites")
        return row["cnt"]

    async def save(self, title: str, artist: str, station_name: str = "",
                   cover_url: str | None = None, stream_url: str | None = None) -> dict | None:
        """Save a radio favorite. Deduplicates by (title, artist)."""
        if not title:
            return None
        try:
            await self._db.execute(
                """INSERT INTO radio_favorites
                   (title, artist, station_name, cover_url, stream_url)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (title, artist) DO NOTHING""",
                (title, artist, station_name, cover_url, stream_url),
            )
            await self._db.commit()
            row = await self._db.fetchone(
                "SELECT * FROM radio_favorites WHERE title = ? AND artist = ?",
                (title, artist),
            )
            return dict(row) if row else None
        except Exception:
            return None

    async def is_favorite(self, title: str, artist: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM radio_favorites WHERE title = ? AND artist = ?",
            (title, artist),
        )
        return row is not None

    async def delete(self, fav_id: int) -> None:
        await self._db.execute("DELETE FROM radio_favorites WHERE id = ?", (fav_id,))
        await self._db.commit()

    async def clear(self) -> None:
        await self._db.execute("DELETE FROM radio_favorites")
        await self._db.commit()

    async def export_csv(self) -> str:
        """Export favorites as CSV (Artist,Title format for Soundiiz)."""
        rows = await self._db.fetchall(
            "SELECT artist, title, station_name, saved_at FROM radio_favorites ORDER BY saved_at DESC"
        )
        lines = ["Artist,Title,Station,Date"]
        for r in rows:
            artist = str(r["artist"]).replace('"', '""')
            title = str(r["title"]).replace('"', '""')
            station = str(r["station_name"]).replace('"', '""')
            lines.append(f'"{artist}","{title}","{station}","{r["saved_at"]}"')
        return "\n".join(lines)

from __future__ import annotations

from typing import Optional

import structlog

from tune_server.db.engine import Database
from tune_server.models import Album, Artist, Playlist, RadioStation, RadioStationCreate, SearchResult, Track

logger = structlog.get_logger()


def _row_to_artist(row) -> Artist:
    return Artist(
        id=row["id"],
        name=row["name"],
        sort_name=row["sort_name"],
        musicbrainz_id=row["musicbrainz_id"],
        discogs_id=row["discogs_id"],
        bio=row["bio"],
        image_path=row["image_path"],
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
        artist_name=row["artist_name"] if "artist_name" in keys else None,
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
    return Track(
        id=row["id"],
        title=row["title"],
        album_id=row["album_id"],
        album_title=row["album_title"] if "album_title" in row.keys() else None,
        artist_id=row["artist_id"],
        artist_name=row["artist_name"] if "artist_name" in row.keys() else None,
        disc_number=row["disc_number"],
        track_number=row["track_number"],
        duration_ms=row["duration_ms"],
        file_path=row["file_path"],
        format=row["format"],
        sample_rate=row["sample_rate"],
        bit_depth=row["bit_depth"],
        channels=row["channels"],
        cover_path=row["cover_path"] if "cover_path" in row.keys() else None,
        source=row["source"],
        source_id=row["source_id"],
        isrc=row["isrc"] if "isrc" in row.keys() else None,
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

    async def list(self, limit: int = 100, offset: int = 0) -> list[Artist]:
        rows = await self._db.fetchall(
            "SELECT * FROM artists ORDER BY sort_name, name LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [_row_to_artist(r) for r in rows]

    async def count(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) as cnt FROM artists")
        return row["cnt"]

    async def list_initial_letters(self) -> list[tuple[str, int]]:
        rows = await self._db.fetchall(
            """SELECT
                 CASE WHEN UPPER(SUBSTR(COALESCE(sort_name, name), 1, 1)) BETWEEN 'A' AND 'Z'
                      THEN UPPER(SUBSTR(COALESCE(sort_name, name), 1, 1)) ELSE '#' END AS letter,
                 COUNT(*) AS cnt
               FROM artists GROUP BY letter ORDER BY letter""",
        )
        return [(r["letter"], r["cnt"]) for r in rows]

    async def list_by_letter(self, letter: str, limit: int = 500, offset: int = 0) -> list[Artist]:
        if letter == "#":
            where = "UPPER(SUBSTR(COALESCE(sort_name, name), 1, 1)) NOT BETWEEN 'A' AND 'Z'"
            params: tuple = (limit, offset)
        else:
            where = "UPPER(SUBSTR(COALESCE(sort_name, name), 1, 1)) = ?"
            params = (letter.upper(), limit, offset)
        rows = await self._db.fetchall(
            f"SELECT * FROM artists WHERE {where} ORDER BY sort_name, name LIMIT ? OFFSET ?",
            params,
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
    _SELECT = """SELECT al.*, ar.name as artist_name,
               tq.max_sample_rate, tq.max_bit_depth, tq.dominant_format
               FROM albums al
               LEFT JOIN artists ar ON al.artist_id = ar.id
               LEFT JOIN (
                   SELECT album_id,
                          MAX(sample_rate) as max_sample_rate,
                          MAX(bit_depth) as max_bit_depth,
                          (SELECT format FROM tracks t2 WHERE t2.album_id = t.album_id
                           GROUP BY format ORDER BY COUNT(*) DESC LIMIT 1) as dominant_format
                   FROM tracks t WHERE album_id IS NOT NULL
                   GROUP BY album_id
               ) tq ON tq.album_id = al.id"""

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
               track_count, cover_path, source, source_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (album.title, album.artist_id, album.year, album.genre,
             album.disc_count, album.track_count, album.cover_path,
             album.source, album.source_id),
        )
        await self._db.commit()
        return result.lastrowid

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
        if getattr(self._db, 'engine_name', 'sqlite') == 'postgres':
            rows = await self._db.fetchall(
                """SELECT al.*, ar.name as artist_name FROM albums al
                   LEFT JOIN artists ar ON al.artist_id = ar.id
                   WHERE al.fts_vector @@ plainto_tsquery('simple', ?)
                   ORDER BY ts_rank(al.fts_vector, plainto_tsquery('simple', ?)) DESC
                   LIMIT ?""",
                (query, query, limit),
            )
        else:
            rows = await self._db.fetchall(
                """SELECT al.*, ar.name as artist_name FROM albums al
                   LEFT JOIN artists ar ON al.artist_id = ar.id
                   JOIN albums_fts fts ON al.id = fts.rowid
                   WHERE albums_fts MATCH ? LIMIT ?""",
                (query + "*", limit),
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

    async def list(self, limit: int = 100, offset: int = 0) -> list[Track]:
        rows = await self._db.fetchall(
            f"{self._SELECT} ORDER BY t.title LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [_row_to_track(r) for r in rows]

    async def list_by_album(self, album_id: int) -> list[Track]:
        rows = await self._db.fetchall(
            f"{self._SELECT} WHERE t.album_id = ? ORDER BY t.disc_number, t.track_number",
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
        if getattr(self._db, 'engine_name', 'sqlite') == 'postgres':
            rows = await self._db.fetchall(
                """SELECT t.*, al.title as album_title, ar.name as artist_name
                    FROM tracks t
                    LEFT JOIN albums al ON t.album_id = al.id
                    LEFT JOIN artists ar ON t.artist_id = ar.id
                    WHERE t.fts_vector @@ plainto_tsquery('simple', ?)
                    ORDER BY ts_rank(t.fts_vector, plainto_tsquery('simple', ?)) DESC
                    LIMIT ?""",
                (query, query, limit),
            )
        else:
            rows = await self._db.fetchall(
                """SELECT t.*, al.title as album_title, ar.name as artist_name
                    FROM tracks t
                    LEFT JOIN albums al ON t.album_id = al.id
                    LEFT JOIN artists ar ON t.artist_id = ar.id
                    JOIN tracks_fts fts ON t.id = fts.rowid
                    WHERE tracks_fts MATCH ? LIMIT ?""",
                (query + "*", limit),
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
        for i, track_id in enumerate(track_ids):
            await self._db.execute(
                """INSERT INTO play_queue (zone_id, track_id, position, is_current)
                   VALUES (?, ?, ?, ?)""",
                (zone_id, track_id, i, 1 if i == 0 else 0),
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

        for i, track_id in enumerate(track_ids):
            await self._db.execute(
                "INSERT INTO play_queue (zone_id, track_id, position) VALUES (?, ?, ?)",
                (zone_id, track_id, position + i),
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

    async def create(self, name: str, output_type: str, output_device_id: str = None) -> int:
        result = await self._db.execute(
            "INSERT INTO zones (name, output_type, output_device_id) VALUES (?, ?, ?) RETURNING id",
            (name, output_type, output_device_id),
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

    async def add_tracks(self, playlist_id: int, track_ids: list[int], position: Optional[int] = None) -> None:
        if position is not None:
            await self._db.execute(
                "UPDATE playlist_tracks SET position = position + ? WHERE playlist_id = ? AND position >= ?",
                (len(track_ids), playlist_id, position),
            )
        else:
            row = await self._db.fetchone(
                "SELECT COALESCE(MAX(position), -1) + 1 as next_pos FROM playlist_tracks WHERE playlist_id = ?",
                (playlist_id,),
            )
            position = row["next_pos"]

        for i, track_id in enumerate(track_ids):
            await self._db.execute(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                (playlist_id, track_id, position + i),
            )
        await self._db.commit()

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

    async def reorder_tracks(self, playlist_id: int, track_ids: list[int]) -> None:
        await self._db.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id = ?",
            (playlist_id,),
        )
        for i, track_id in enumerate(track_ids):
            await self._db.execute(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                (playlist_id, track_id, i),
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


async def full_text_search(db: Database, query: str, limit: int = 50) -> SearchResult:
    track_repo = TrackRepo(db)
    album_repo = AlbumRepo(db)
    artist_repo = ArtistRepo(db)

    tracks = await track_repo.search(query, limit)
    albums = await album_repo.search(query, limit)
    artists = await artist_repo.search(query, limit)

    # Enrich: also fetch albums/tracks for matching artists
    seen_album_ids = {a.id for a in albums if a.id}
    seen_track_ids = {t.id for t in tracks if t.id}
    for artist in artists:
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

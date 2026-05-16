"""Import library data from Roon, Plex, or M3U/PLS playlists.

Matching strategy:
  1. file_path exact match (fastest, most reliable)
  2. Fuzzy: LOWER(title) + LOWER(artist) + LOWER(album)

Data policy — never overwrite existing Tune data:
  - play_count += imported count
  - rating: keep the higher value
  - date_added: keep the earliest
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Optional

import structlog

from tune_server.api.deps import deps

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class ImportedTrack:
    """One row parsed from a Roon CSV, Plex XML, or playlist file."""
    file_path: Optional[str] = None
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    genre: Optional[str] = None
    play_count: int = 0
    rating: Optional[int] = None  # 1-5
    date_added: Optional[str] = None
    last_played: Optional[str] = None
    playlist_name: Optional[str] = None  # for playlist imports


@dataclass
class MatchResult:
    imported_title: str
    imported_artist: Optional[str]
    imported_album: Optional[str]
    tune_track_id: Optional[int] = None
    matched: bool = False
    match_method: Optional[str] = None  # "file_path" | "fuzzy"


@dataclass
class ImportReport:
    source: str  # "roon", "plex", "playlist"
    total_rows: int = 0
    matched: int = 0
    unmatched: int = 0
    play_counts_updated: int = 0
    ratings_updated: int = 0
    history_entries_added: int = 0
    playlists_created: int = 0
    details: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_ROON_PATH_HEADERS = {"file path", "filepath", "path", "file_path", "location", "file"}
_ROON_TITLE_HEADERS = {"title", "track title", "track_title", "track", "name"}
_ROON_ARTIST_HEADERS = {"artist", "artist name", "artist_name", "performers"}
_ROON_ALBUM_HEADERS = {"album", "album title", "album_title"}
_ROON_GENRE_HEADERS = {"genre", "genres"}
_ROON_PLAY_COUNT_HEADERS = {"play count", "play_count", "plays", "playcount"}
_ROON_RATING_HEADERS = {"rating", "stars", "user rating", "user_rating"}
_ROON_DATE_ADDED_HEADERS = {"date added", "date_added", "added", "added_date", "import date"}
_ROON_LAST_PLAYED_HEADERS = {"last played", "last_played", "lastplayed"}


def _detect_header(headers_lower: list[str], candidates: set[str]) -> Optional[int]:
    """Return the column index matching any candidate, or None."""
    for i, h in enumerate(headers_lower):
        if h in candidates:
            return i
    return None


def parse_roon_csv(raw: str) -> list[ImportedTrack]:
    """Parse a Roon library export CSV.  Flexible header detection."""
    reader = csv.reader(io.StringIO(raw), delimiter=",")
    try:
        raw_headers = next(reader)
    except StopIteration:
        return []

    # Strip BOM and whitespace
    headers = [h.strip().lstrip("﻿").lower() for h in raw_headers]

    # Also try semicolon if we got a single column
    if len(headers) <= 1 and ";" in raw:
        reader = csv.reader(io.StringIO(raw), delimiter=";")
        try:
            raw_headers = next(reader)
        except StopIteration:
            return []
        headers = [h.strip().lstrip("﻿").lower() for h in raw_headers]

    col_path = _detect_header(headers, _ROON_PATH_HEADERS)
    col_title = _detect_header(headers, _ROON_TITLE_HEADERS)
    col_artist = _detect_header(headers, _ROON_ARTIST_HEADERS)
    col_album = _detect_header(headers, _ROON_ALBUM_HEADERS)
    col_genre = _detect_header(headers, _ROON_GENRE_HEADERS)
    col_play = _detect_header(headers, _ROON_PLAY_COUNT_HEADERS)
    col_rating = _detect_header(headers, _ROON_RATING_HEADERS)
    col_added = _detect_header(headers, _ROON_DATE_ADDED_HEADERS)
    col_last = _detect_header(headers, _ROON_LAST_PLAYED_HEADERS)

    if col_title is None and col_path is None:
        logger.warning("import_roon_csv_no_usable_headers", headers=headers)
        return []

    tracks: list[ImportedTrack] = []
    for row in reader:
        if not row or all(c.strip() == "" for c in row):
            continue
        t = ImportedTrack()
        try:
            if col_path is not None and col_path < len(row):
                t.file_path = row[col_path].strip() or None
            if col_title is not None and col_title < len(row):
                t.title = row[col_title].strip() or None
            if col_artist is not None and col_artist < len(row):
                t.artist = row[col_artist].strip() or None
            if col_album is not None and col_album < len(row):
                t.album = row[col_album].strip() or None
            if col_genre is not None and col_genre < len(row):
                t.genre = row[col_genre].strip() or None
            if col_play is not None and col_play < len(row):
                val = row[col_play].strip()
                t.play_count = int(val) if val else 0
            if col_rating is not None and col_rating < len(row):
                val = row[col_rating].strip()
                if val:
                    r = int(float(val))
                    t.rating = max(1, min(5, r)) if r > 0 else None
            if col_added is not None and col_added < len(row):
                t.date_added = row[col_added].strip() or None
            if col_last is not None and col_last < len(row):
                t.last_played = row[col_last].strip() or None
        except (ValueError, IndexError):
            pass
        if t.title or t.file_path:
            tracks.append(t)

    return tracks


def parse_plex_xml(raw: str) -> list[ImportedTrack]:
    """Parse a Plex library XML export."""
    tracks: list[ImportedTrack] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        logger.warning("import_plex_xml_parse_error", error=str(exc))
        return []

    # Plex XML: <MediaContainer> -> <Track> or <Video>
    # Common attributes: title, parentTitle (album), grandparentTitle (artist),
    # viewCount, userRating, addedAt (unix), file (in <Media><Part>)
    for elem in root.iter():
        tag = elem.tag.lower() if elem.tag else ""
        if tag not in ("track", "video"):
            continue

        t = ImportedTrack()
        t.title = elem.get("title")
        t.artist = elem.get("grandparentTitle") or elem.get("originalTitle")
        t.album = elem.get("parentTitle")
        t.genre = elem.get("genre")

        # viewCount -> play_count
        vc = elem.get("viewCount")
        if vc:
            try:
                t.play_count = int(vc)
            except ValueError:
                pass

        # userRating (0-10 scale) -> 1-5
        ur = elem.get("userRating")
        if ur:
            try:
                r = int(float(ur) / 2.0 + 0.5)
                t.rating = max(1, min(5, r)) if r > 0 else None
            except ValueError:
                pass

        # addedAt (unix timestamp)
        at = elem.get("addedAt")
        if at:
            try:
                t.date_added = datetime.fromtimestamp(int(at)).isoformat()
            except (ValueError, OSError):
                pass

        # file path from <Media><Part file="...">
        for media in elem.findall("Media"):
            for part in media.findall("Part"):
                fp = part.get("file")
                if fp:
                    t.file_path = fp
                    break
            if t.file_path:
                break

        if t.title or t.file_path:
            tracks.append(t)

    return tracks


def parse_m3u(raw: str, playlist_name: str | None = None) -> list[ImportedTrack]:
    """Parse M3U/M3U8 playlist file."""
    tracks: list[ImportedTrack] = []
    current_title: str | None = None
    current_artist: str | None = None

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF:"):
            # #EXTINF:duration,Artist - Title  or  #EXTINF:duration,Title
            info = line.split(",", 1)
            if len(info) > 1:
                display = info[1].strip()
                if " - " in display:
                    parts = display.split(" - ", 1)
                    current_artist = parts[0].strip()
                    current_title = parts[1].strip()
                else:
                    current_title = display
            continue
        if line.startswith("#"):
            continue
        # This is a file path
        t = ImportedTrack(
            file_path=line,
            title=current_title,
            artist=current_artist,
            playlist_name=playlist_name,
        )
        tracks.append(t)
        current_title = None
        current_artist = None

    return tracks


def parse_pls(raw: str, playlist_name: str | None = None) -> list[ImportedTrack]:
    """Parse PLS playlist file."""
    tracks: list[ImportedTrack] = []
    files: dict[str, str] = {}
    titles: dict[str, str] = {}

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("[") or line.lower().startswith("numberofentries"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key.startswith("file"):
            idx = key[4:]
            files[idx] = value
        elif key.startswith("title"):
            idx = key[5:]
            titles[idx] = value

    for idx in sorted(files.keys(), key=lambda x: int(x) if x.isdigit() else 0):
        fp = files[idx]
        display = titles.get(idx, "")
        artist = None
        title = display
        if " - " in display:
            parts = display.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()
        t = ImportedTrack(
            file_path=fp,
            title=title or None,
            artist=artist,
            playlist_name=playlist_name,
        )
        tracks.append(t)

    return tracks


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------

async def _match_tracks(imported: list[ImportedTrack]) -> list[MatchResult]:
    """Match imported tracks against the Tune library.

    1. Batch file_path match
    2. Fall back to fuzzy LOWER(title) + LOWER(artist) + LOWER(album)
    """
    results: list[MatchResult] = []
    db = deps.db
    if db is None:
        return results

    # Build file_path index from Tune library for fast lookups
    all_tracks = await db.fetchall(
        "SELECT id, file_path, LOWER(title) as l_title, "
        "LOWER(COALESCE((SELECT name FROM artists WHERE id = tracks.artist_id), '')) as l_artist, "
        "LOWER(COALESCE((SELECT title FROM albums WHERE id = tracks.album_id), '')) as l_album "
        "FROM tracks WHERE source = 'local'"
    )

    path_index: dict[str, int] = {}
    fuzzy_index: dict[tuple[str, str, str], int] = {}
    # Also index by filename only for cross-platform path matching
    filename_index: dict[str, int] = {}

    for row in all_tracks:
        tid = row["id"]
        fp = row["file_path"]
        if fp:
            path_index[fp] = tid
            # Index by filename for cross-platform matching
            fname = PurePosixPath(fp).name.lower()
            if fname not in filename_index:
                filename_index[fname] = tid

        key = (row["l_title"] or "", row["l_artist"] or "", row["l_album"] or "")
        if key[0]:  # at least a title
            fuzzy_index[key] = tid

    for imp in imported:
        mr = MatchResult(
            imported_title=imp.title or "(sans titre)",
            imported_artist=imp.artist,
            imported_album=imp.album,
        )

        # 1. Exact file_path
        if imp.file_path and imp.file_path in path_index:
            mr.tune_track_id = path_index[imp.file_path]
            mr.matched = True
            mr.match_method = "file_path"
            results.append(mr)
            continue

        # 1b. Cross-platform: try matching just the filename
        if imp.file_path:
            # Normalize Windows backslashes
            normalized = imp.file_path.replace("\\", "/")
            if normalized in path_index:
                mr.tune_track_id = path_index[normalized]
                mr.matched = True
                mr.match_method = "file_path"
                results.append(mr)
                continue

            fname = PurePosixPath(normalized).name.lower()
            if fname in filename_index:
                mr.tune_track_id = filename_index[fname]
                mr.matched = True
                mr.match_method = "file_path"
                results.append(mr)
                continue

        # 2. Fuzzy: LOWER(title) + LOWER(artist) + LOWER(album)
        key = (
            (imp.title or "").lower().strip(),
            (imp.artist or "").lower().strip(),
            (imp.album or "").lower().strip(),
        )
        if key[0] and key in fuzzy_index:
            mr.tune_track_id = fuzzy_index[key]
            mr.matched = True
            mr.match_method = "fuzzy"
            results.append(mr)
            continue

        # 2b. Relax: title + artist only
        if key[0] and key[1]:
            for fk, fid in fuzzy_index.items():
                if fk[0] == key[0] and fk[1] == key[1]:
                    mr.tune_track_id = fid
                    mr.matched = True
                    mr.match_method = "fuzzy"
                    break
        if mr.matched:
            results.append(mr)
            continue

        # 2c. Relax: title only (less reliable, skip if ambiguous)
        if key[0]:
            candidates = [fid for fk, fid in fuzzy_index.items() if fk[0] == key[0]]
            if len(candidates) == 1:
                mr.tune_track_id = candidates[0]
                mr.matched = True
                mr.match_method = "fuzzy"

        results.append(mr)

    return results


# ---------------------------------------------------------------------------
# Import executor
# ---------------------------------------------------------------------------

async def apply_import(
    imported: list[ImportedTrack],
    matches: list[MatchResult],
    source: str,
    *,
    dry_run: bool = False,
) -> ImportReport:
    """Apply matched import data to the Tune library.

    Never overwrites existing data — only fills in blanks.
    """
    report = ImportReport(source=source, total_rows=len(imported))
    db = deps.db
    if db is None:
        return report

    history_repo = deps.history_repo

    for imp, mr in zip(imported, matches):
        detail = {
            "title": mr.imported_title,
            "artist": mr.imported_artist,
            "album": mr.imported_album,
            "matched": mr.matched,
            "match_method": mr.match_method,
            "tune_track_id": mr.tune_track_id,
        }

        if mr.matched:
            report.matched += 1
        else:
            report.unmatched += 1

        report.details.append(detail)

        if not mr.matched or mr.tune_track_id is None or dry_run:
            continue

        # Insert play_count entries into playback_history
        if imp.play_count > 0 and history_repo:
            for _ in range(imp.play_count):
                await history_repo.record(
                    track_id=mr.tune_track_id,
                    zone_id=None,
                    track_title=imp.title or "",
                    artist_name=imp.artist,
                    album_title=imp.album,
                    cover_path=None,
                    duration_ms=None,
                    listened_ms=None,
                    source=f"import_{source}",
                )
            report.play_counts_updated += 1

        # Album rating (use album_rating_repo if available)
        if imp.rating and imp.rating >= 1:
            album_rating_repo = deps.album_rating_repo
            if album_rating_repo:
                # Find album_id for this track
                track_row = await db.fetchone(
                    "SELECT album_id FROM tracks WHERE id = ?",
                    (mr.tune_track_id,),
                )
                if track_row and track_row["album_id"]:
                    # Only set rating if no existing rating or import rating is higher
                    existing = await album_rating_repo.get(track_row["album_id"])
                    existing_rating = existing.get("rating", 0) if existing else 0
                    if imp.rating > (existing_rating or 0):
                        await album_rating_repo.rate(
                            track_row["album_id"],
                            imp.rating,
                        )
                        report.ratings_updated += 1

    return report


async def import_playlists_from_tracks(
    imported: list[ImportedTrack],
    matches: list[MatchResult],
    *,
    dry_run: bool = False,
) -> int:
    """Create playlists from matched tracks that have a playlist_name."""
    playlist_repo = deps.playlist_repo
    if not playlist_repo or dry_run:
        return 0

    # Group by playlist name
    playlists: dict[str, list[int]] = {}
    for imp, mr in zip(imported, matches):
        if not mr.matched or mr.tune_track_id is None:
            continue
        name = imp.playlist_name
        if not name:
            continue
        playlists.setdefault(name, []).append(mr.tune_track_id)

    created = 0
    for name, track_ids in playlists.items():
        if not track_ids:
            continue
        playlist_id = await playlist_repo.create(
            name=name,
            description=f"Import depuis fichier playlist",
        )
        await playlist_repo.add_tracks(playlist_id, track_ids)
        created += 1
        logger.info("import_playlist_created", name=name, tracks=len(track_ids))

    return created


# ---------------------------------------------------------------------------
# High-level orchestrators (called from API routes)
# ---------------------------------------------------------------------------

async def run_roon_import(
    csv_content: str,
    *,
    dry_run: bool = False,
) -> ImportReport:
    """Parse Roon CSV and import into Tune."""
    imported = parse_roon_csv(csv_content)
    if not imported:
        return ImportReport(source="roon")

    matches = await _match_tracks(imported)
    report = await apply_import(imported, matches, "roon", dry_run=dry_run)
    return report


async def run_plex_import(
    xml_content: str,
    *,
    dry_run: bool = False,
) -> ImportReport:
    """Parse Plex XML and import into Tune."""
    imported = parse_plex_xml(xml_content)
    if not imported:
        return ImportReport(source="plex")

    matches = await _match_tracks(imported)
    report = await apply_import(imported, matches, "plex", dry_run=dry_run)
    return report


async def run_playlist_import(
    content: str,
    filename: str,
    *,
    dry_run: bool = False,
) -> ImportReport:
    """Parse M3U/M3U8/PLS playlist and import into Tune."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    playlist_name = filename.rsplit(".", 1)[0] if "." in filename else filename

    if ext == "pls":
        imported = parse_pls(content, playlist_name=playlist_name)
    else:
        imported = parse_m3u(content, playlist_name=playlist_name)

    if not imported:
        return ImportReport(source="playlist")

    matches = await _match_tracks(imported)
    report = await apply_import(imported, matches, "playlist", dry_run=dry_run)
    playlists_created = await import_playlists_from_tracks(
        imported, matches, dry_run=dry_run,
    )
    report.playlists_created = playlists_created
    return report

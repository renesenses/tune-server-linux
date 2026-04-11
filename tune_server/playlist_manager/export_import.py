"""Export/Import playlists — CSV, XSPF, Text, JSON formats."""

from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class PlaylistTrackData:
    title: str
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    source_id: str = ""
    isrc: str = ""


def export_csv(name: str, tracks: list[dict]) -> str:
    """Export playlist as CSV string."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Title", "Artist", "Album", "Duration (s)", "ISRC"])
    for t in tracks:
        writer.writerow([
            t.get("title", ""),
            t.get("artist_name", t.get("artist", "")),
            t.get("album_title", t.get("album", "")),
            t.get("duration_ms", 0) // 1000 if t.get("duration_ms") else "",
            t.get("isrc", ""),
        ])
    return output.getvalue()


def export_json(name: str, tracks: list[dict]) -> str:
    """Export playlist as JSON string."""
    data = {
        "playlist": name,
        "track_count": len(tracks),
        "tracks": [
            {
                "title": t.get("title", ""),
                "artist": t.get("artist_name", t.get("artist", "")),
                "album": t.get("album_title", t.get("album", "")),
                "duration_ms": t.get("duration_ms", 0),
                "isrc": t.get("isrc", ""),
            }
            for t in tracks
        ],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def export_text(name: str, tracks: list[dict]) -> str:
    """Export playlist as plain text (one track per line)."""
    lines = [f"# {name}", f"# {len(tracks)} tracks", ""]
    for i, t in enumerate(tracks, 1):
        artist = t.get("artist_name", t.get("artist", ""))
        title = t.get("title", "")
        album = t.get("album_title", t.get("album", ""))
        line = f"{i}. {artist} - {title}"
        if album:
            line += f" [{album}]"
        lines.append(line)
    return "\n".join(lines)


def export_xspf(name: str, tracks: list[dict]) -> str:
    """Export playlist as XSPF (XML Shareable Playlist Format)."""
    playlist = ET.Element("playlist", version="1", xmlns="http://xspf.org/ns/0/")
    ET.SubElement(playlist, "title").text = name

    tracklist = ET.SubElement(playlist, "trackList")
    for t in tracks:
        track_el = ET.SubElement(tracklist, "track")
        ET.SubElement(track_el, "title").text = t.get("title", "")
        ET.SubElement(track_el, "creator").text = t.get("artist_name", t.get("artist", ""))
        ET.SubElement(track_el, "album").text = t.get("album_title", t.get("album", ""))
        duration = t.get("duration_ms", 0)
        if duration:
            ET.SubElement(track_el, "duration").text = str(duration)

    return ET.tostring(playlist, encoding="unicode", xml_declaration=True)


def export_playlist(name: str, tracks: list[dict], fmt: str) -> tuple[str, str, str]:
    """Export a playlist in the given format.

    Returns: (content, content_type, filename)
    """
    if fmt == "csv":
        return export_csv(name, tracks), "text/csv", f"{name}.csv"
    elif fmt == "json":
        return export_json(name, tracks), "application/json", f"{name}.json"
    elif fmt == "text":
        return export_text(name, tracks), "text/plain", f"{name}.txt"
    elif fmt == "xspf":
        return export_xspf(name, tracks), "application/xspf+xml", f"{name}.xspf"
    else:
        raise ValueError(f"Unknown format: {fmt}")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_csv(content: str) -> tuple[str, list[PlaylistTrackData]]:
    """Import tracks from CSV. Returns (playlist_name, tracks)."""
    reader = csv.reader(io.StringIO(content))
    tracks = []
    header = None
    for row in reader:
        if not row:
            continue
        if header is None:
            header = [h.strip().lower() for h in row]
            continue
        data = dict(zip(header, row))
        tracks.append(PlaylistTrackData(
            title=data.get("title", ""),
            artist=data.get("artist", ""),
            album=data.get("album", ""),
            duration_ms=int(float(data.get("duration (s)", "0")) * 1000) if data.get("duration (s)") else 0,
            isrc=data.get("isrc", ""),
        ))
    return "Imported Playlist", tracks


def import_json(content: str) -> tuple[str, list[PlaylistTrackData]]:
    """Import tracks from JSON."""
    data = json.loads(content)
    name = data.get("playlist", "Imported Playlist")
    tracks = [
        PlaylistTrackData(
            title=t.get("title", ""),
            artist=t.get("artist", ""),
            album=t.get("album", ""),
            duration_ms=t.get("duration_ms", 0),
            isrc=t.get("isrc", ""),
        )
        for t in data.get("tracks", [])
    ]
    return name, tracks


def import_text(content: str) -> tuple[str, list[PlaylistTrackData]]:
    """Import tracks from plain text (artist - title format)."""
    lines = content.strip().split("\n")
    name = "Imported Playlist"
    tracks = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if name == "Imported Playlist" and len(line) > 2:
                name = line[2:].strip()
            continue
        # Strip leading number: "1. Artist - Title [Album]"
        import re
        line = re.sub(r"^\d+\.\s*", "", line)
        # Extract album from [brackets]
        album = ""
        album_match = re.search(r"\[([^\]]+)\]", line)
        if album_match:
            album = album_match.group(1)
            line = line[:album_match.start()].strip()
        # Split artist - title
        if " - " in line:
            artist, title = line.split(" - ", 1)
        else:
            artist, title = "", line
        tracks.append(PlaylistTrackData(
            title=title.strip(),
            artist=artist.strip(),
            album=album,
        ))
    return name, tracks


def import_xspf(content: str) -> tuple[str, list[PlaylistTrackData]]:
    """Import tracks from XSPF."""
    root = ET.fromstring(content)
    ns = {"": "http://xspf.org/ns/0/"}

    name_el = root.find("title", ns) or root.find("{http://xspf.org/ns/0/}title")
    name = name_el.text if name_el is not None and name_el.text else "Imported Playlist"

    tracks = []
    tracklist = root.find("{http://xspf.org/ns/0/}trackList") or root.find("trackList", ns)
    if tracklist is not None:
        for track_el in tracklist.findall("{http://xspf.org/ns/0/}track"):
            title_el = track_el.find("{http://xspf.org/ns/0/}title")
            creator_el = track_el.find("{http://xspf.org/ns/0/}creator")
            album_el = track_el.find("{http://xspf.org/ns/0/}album")
            dur_el = track_el.find("{http://xspf.org/ns/0/}duration")
            tracks.append(PlaylistTrackData(
                title=title_el.text if title_el is not None else "",
                artist=creator_el.text if creator_el is not None else "",
                album=album_el.text if album_el is not None else "",
                duration_ms=int(dur_el.text) if dur_el is not None and dur_el.text else 0,
            ))
    return name, tracks


def import_playlist(content: str, fmt: str) -> tuple[str, list[PlaylistTrackData]]:
    """Import a playlist from the given format.

    Returns: (playlist_name, tracks)
    """
    if fmt == "csv":
        return import_csv(content)
    elif fmt == "json":
        return import_json(content)
    elif fmt == "text":
        return import_text(content)
    elif fmt == "xspf":
        return import_xspf(content)
    else:
        raise ValueError(f"Unknown format: {fmt}")

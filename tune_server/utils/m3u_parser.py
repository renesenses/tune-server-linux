"""Advanced M3U/M3U8 parser for music playlist import/export."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class M3UEntry:
    """A single entry parsed from an M3U file."""
    path: str  # file path or URL
    title: str | None = None
    artist: str | None = None
    duration_s: int = -1  # seconds, -1 = unknown
    is_url: bool = False
    extra_attrs: dict[str, str] = field(default_factory=dict)


def parse_m3u_content(raw_bytes: bytes, force_utf8: bool = False) -> list[M3UEntry]:
    """Parse M3U/M3U8 content from raw bytes.

    M3U8 files are always UTF-8.  Plain M3U may be Latin-1 (ISO-8859-1).
    If force_utf8 is True (or the content starts with #EXTM3U and decodes
    cleanly as UTF-8), we use UTF-8; otherwise we fall back to Latin-1.
    """
    text = _decode(raw_bytes, force_utf8)
    return _parse_text(text)


def _decode(raw: bytes, force_utf8: bool) -> str:
    """Decode raw bytes to string, choosing encoding automatically."""
    # BOM detection
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    if force_utf8:
        return raw.decode("utf-8", errors="replace")
    # Try UTF-8 first
    try:
        text = raw.decode("utf-8")
        return text
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _parse_text(text: str) -> list[M3UEntry]:
    """Parse M3U text into entries."""
    entries: list[M3UEntry] = []
    pending_title: str | None = None
    pending_artist: str | None = None
    pending_duration: int = -1
    pending_attrs: dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        upper = line.upper()
        if upper.startswith("#EXTM3U"):
            continue

        if upper.startswith("#EXTINF:"):
            pending_duration, pending_artist, pending_title, pending_attrs = _parse_extinf(line)
            continue

        # Skip other comments/directives
        if line.startswith("#"):
            continue

        # This is a path or URL line
        is_url = _is_url(line)
        entry = M3UEntry(
            path=line,
            title=pending_title,
            artist=pending_artist,
            duration_s=pending_duration,
            is_url=is_url,
            extra_attrs=pending_attrs,
        )
        entries.append(entry)

        # Reset pending
        pending_title = None
        pending_artist = None
        pending_duration = -1
        pending_attrs = {}

    return entries


_ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')


def _parse_extinf(line: str) -> tuple[int, str | None, str | None, dict[str, str]]:
    """Parse an #EXTINF line.

    Formats:
        #EXTINF:123,Artist - Title
        #EXTINF:123,Title
        #EXTINF:-1 tvg-logo="..." group-title="...",Station Name
    """
    # Strip the #EXTINF: prefix
    rest = line[8:]  # len("#EXTINF:") == 8

    # Extract inline attributes (key="value") before the comma
    attrs: dict[str, str] = {}
    for m in _ATTR_RE.finditer(rest):
        attrs[m.group(1).lower()] = m.group(2)

    # Split on the LAST comma that separates duration+attrs from the display name
    # The duration is always the first token before any space or comma
    comma_idx = rest.find(",")
    if comma_idx == -1:
        # No comma -- just a duration
        duration_s = _parse_duration_token(rest.split()[0] if rest.split() else "-1")
        return duration_s, None, None, attrs

    before_comma = rest[:comma_idx]
    display_name = rest[comma_idx + 1:].strip()

    # Duration is the first numeric token in before_comma
    dur_token = before_comma.split()[0] if before_comma.split() else "-1"
    duration_s = _parse_duration_token(dur_token)

    # Parse "Artist - Title" from display name
    artist, title = _split_artist_title(display_name)

    return duration_s, artist, title, attrs


def _parse_duration_token(token: str) -> int:
    """Parse a duration token, returning seconds or -1."""
    # Remove trailing comma if present
    token = token.rstrip(",").strip()
    try:
        val = int(float(token))
        return val if val >= 0 else -1
    except (ValueError, OverflowError):
        return -1


def _split_artist_title(display: str) -> tuple[str | None, str | None]:
    """Split 'Artist - Title' into (artist, title).

    If there is no separator, returns (None, display).
    """
    if not display:
        return None, None

    # Try " - " separator (most common in M3U)
    for sep in (" - ", " -- ", " – ", " — "):
        if sep in display:
            parts = display.split(sep, 1)
            return parts[0].strip() or None, parts[1].strip() or None

    # No separator found -- the whole string is the title
    return None, display


def _is_url(path: str) -> bool:
    """Check if a path is an HTTP/HTTPS/RTSP URL."""
    lower = path.lower()
    return lower.startswith(("http://", "https://", "rtsp://", "mms://", "rtmp://"))


def generate_m3u8(entries: list[dict]) -> str:
    """Generate M3U8 content from a list of track dicts.

    Each dict should have:
        - title: str
        - artist_name: str | None
        - duration_ms: int (milliseconds)
        - file_path: str | None
        - source: str (e.g. "local", "radio")
    """
    lines: list[str] = ["#EXTM3U"]

    for entry in entries:
        title = entry.get("title", "Unknown")
        artist = entry.get("artist_name") or ""
        duration_ms = entry.get("duration_ms", 0)
        duration_s = max(duration_ms // 1000, -1) if duration_ms else -1
        file_path = entry.get("file_path")
        source = entry.get("source", "local")
        source_id = entry.get("source_id")

        # Build display name
        if artist:
            display = f"{artist} - {title}"
        else:
            display = title

        lines.append(f"#EXTINF:{duration_s},{display}")

        # Path: prefer file_path for local, otherwise use a comment
        if file_path:
            lines.append(file_path)
        elif source_id and source not in ("local",):
            # For streaming tracks, include a comment with the source info
            lines.append(f"# {source}:{source_id}")
        else:
            lines.append(f"# {title}")

    lines.append("")  # trailing newline
    return "\n".join(lines)

"""Parsers for M3U and PLS playlist files."""
from __future__ import annotations

import re


def parse_m3u(content: str) -> list[dict]:
    """Parse M3U/M3U8 content, returning [{"name": ..., "url": ...}]."""
    entries: list[dict] = []
    current_name: str | None = None

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF:"):
            # Format: #EXTINF:duration,title
            parts = line.split(",", 1)
            current_name = parts[1].strip() if len(parts) > 1 else None
        elif line.startswith("#"):
            continue
        elif line.startswith(("http://", "https://")):
            name = current_name or _name_from_url(line)
            entries.append({"name": name, "url": line})
            current_name = None

    return entries


def parse_pls(content: str) -> list[dict]:
    """Parse PLS content, returning [{"name": ..., "url": ...}]."""
    files: dict[int, str] = {}
    titles: dict[int, str] = {}

    for line in content.splitlines():
        line = line.strip()
        m = re.match(r"File(\d+)\s*=\s*(.+)", line, re.IGNORECASE)
        if m:
            idx = int(m.group(1))
            url = m.group(2).strip()
            if url.startswith(("http://", "https://")):
                files[idx] = url
            continue
        m = re.match(r"Title(\d+)\s*=\s*(.+)", line, re.IGNORECASE)
        if m:
            titles[int(m.group(1))] = m.group(2).strip()

    entries: list[dict] = []
    for idx in sorted(files):
        url = files[idx]
        name = titles.get(idx) or _name_from_url(url)
        entries.append({"name": name, "url": url})

    return entries


def _name_from_url(url: str) -> str:
    """Extract a readable name from a stream URL."""
    # Take the last path segment, strip extension
    path = url.split("?")[0].rstrip("/")
    segment = path.rsplit("/", 1)[-1]
    name = segment.rsplit(".", 1)[0] if "." in segment else segment
    return name or "Unknown"

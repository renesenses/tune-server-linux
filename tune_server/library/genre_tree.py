"""Genre Tree — user-editable parent → children map for hierarchical genre filtering.

The data model on `albums.genre` stays a flat string (no migration), so
existing libraries keep working unchanged. The tree only changes how
filters / Smart Playlist rules / Smart Collection rules can EXPAND a
parent genre into its descendants.

Example:
    {
      "Jazz": ["Jazz US", "Jazz Europe", "Jazz Manouche",
               "Contemporary Jazz", "Bossa Nova"],
      "Rock": ["Pop-Rock", "Hard Rock", "Punk"],
      "Soul-Funk": ["Soul", "Funk", "R&B-Soul"]
    }

`expand_branch("Jazz")` then returns
{"Jazz", "Jazz US", "Jazz Europe", "Jazz Manouche",
 "Contemporary Jazz", "Bossa Nova"}.

Storage: a JSON file in the same directory as the SQLite DB (or the
configured data dir on PostgreSQL deployments) so it survives restarts
and is shareable / editable. We never put it in the DB itself — the
tree is small (<1 KB), human-readable, and a JSON file is a friendlier
format for the future Settings UI to show + edit + import / export.

Read path is cached for the process lifetime; ``invalidate_cache()``
forces a re-read after a write.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterable

from tune_server.config import settings

_TREE_FILENAME = "genre_tree.json"

# Sensible default — matches the families the metadata enrichment endpoint
# already uses, but as ACTUAL genre names rather than substring keywords.
# A first-install user gets this; from then on it's whatever they save.
_DEFAULT_TREE: dict[str, list[str]] = {
    "Jazz": ["Jazz US", "Jazz Europe", "Jazz Manouche", "Contemporary Jazz",
             "Smooth Jazz", "Bebop", "Cool Jazz", "Free Jazz", "Bossa Nova",
             "Jazz Electro"],
    "Rock": ["Pop-Rock", "Indie Rock", "Hard Rock", "Progressive Rock",
             "Psychedelic Rock", "Punk", "Post-Punk", "Garage Rock"],
    "Pop": ["Synthpop", "Dream Pop", "Indie Pop", "Chamber Pop", "Art Pop"],
    "Soul-Funk": ["Soul", "Funk", "R&B", "R&B-Soul", "Neo-Soul",
                  "Funk / Soul"],
    "Electronic": ["Electro", "Ambient", "Techno", "House", "Trip-Hop",
                   "Downtempo", "IDM", "Chillout"],
    "Classical": ["Modern Classical", "Baroque", "Romantic", "Orchestral",
                  "Chamber Music", "Opera", "Choir"],
    "World Music": ["African", "Latin", "Reggae", "Tango", "Afrobeat",
                    "Tropicalia"],
    "Blues": ["Electric Blues", "Delta Blues"],
    "Folk": ["Indie Folk", "Folk Rock"],
    "Hip-Hop": ["Rap", "Trip-Hop"],
    "Chanson": ["Chanson Francaise", "Chanson Française",
                "Variétés Internationales"],
    "Soundtrack": ["Film Score"],
}


_lock = threading.Lock()
_cache: dict[str, list[str]] | None = None


def _tree_path() -> Path:
    """Return the absolute path to genre_tree.json.

    For SQLite deployments this is the directory of the .db file. For
    PostgreSQL deployments we fall back to ``~/.tune-server/`` so a
    server without local DB still has a single canonical location.
    """
    db_path = getattr(settings, "db_path", "") or ""
    if db_path and db_path != ":memory:":
        parent = Path(db_path).expanduser().resolve().parent
        if parent.exists():
            return parent / _TREE_FILENAME
    fallback = Path.home() / ".tune-server"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback / _TREE_FILENAME


def load() -> dict[str, list[str]]:
    """Return the stored tree, falling back to the default tree on first run."""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        path = _tree_path()
        if not path.is_file():
            _cache = dict(_DEFAULT_TREE)
            _save_unsafe(_cache)
            return _cache
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("genre_tree.json is not a JSON object")
            cleaned: dict[str, list[str]] = {}
            for parent, children in data.items():
                if not isinstance(parent, str) or not parent.strip():
                    continue
                if not isinstance(children, list):
                    continue
                cleaned[parent.strip()] = [
                    str(c).strip() for c in children
                    if isinstance(c, str) and str(c).strip()
                ]
            _cache = cleaned
            return _cache
        except Exception:
            _cache = dict(_DEFAULT_TREE)
            return _cache


def save(tree: dict[str, list[str]]) -> None:
    """Persist the tree to disk + bust the cache."""
    global _cache
    if not isinstance(tree, dict):
        raise ValueError("tree must be a dict")
    cleaned: dict[str, list[str]] = {}
    for parent, children in tree.items():
        if not isinstance(parent, str) or not parent.strip():
            continue
        if not isinstance(children, list):
            continue
        cleaned[parent.strip()] = [
            str(c).strip() for c in children
            if isinstance(c, str) and str(c).strip()
        ]
    with _lock:
        _save_unsafe(cleaned)
        _cache = cleaned


def _save_unsafe(tree: dict[str, list[str]]) -> None:
    path = _tree_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(tree, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def invalidate_cache() -> None:
    """Force the next ``load()`` to re-read from disk."""
    global _cache
    with _lock:
        _cache = None


def expand_branch(genre: str | None) -> set[str]:
    """Return the set of genre names that belong to the same branch.

    For a parent genre (e.g. "Jazz"), the result includes the parent
    itself and all its direct children. For a leaf genre (e.g. "Jazz US")
    that's not a parent in the tree, the result is just {leaf}.

    Case-insensitive matching on the parent lookup, but the returned set
    preserves the original casing (so a UI consumer can render the right
    label). Returns an empty set on None / empty input.
    """
    if not genre:
        return set()
    tree = load()
    g = genre.strip()
    out: set[str] = {g}
    # Case-insensitive parent match.
    g_lower = g.lower()
    for parent, children in tree.items():
        if parent.strip().lower() == g_lower:
            out.add(parent)
            out.update(c for c in children if c)
            break
    return out


def parent_of(child: str | None) -> str | None:
    """Reverse lookup: which parent does this leaf belong to?

    Returns None if the genre is itself a parent or not found.
    """
    if not child:
        return None
    tree = load()
    c_lower = child.strip().lower()
    for parent, children in tree.items():
        for kid in children:
            if kid.strip().lower() == c_lower:
                return parent
    return None


def all_known_genres() -> set[str]:
    """Every genre name mentioned anywhere in the tree (parents + children).

    Useful for offering autocomplete in the UI.
    """
    tree = load()
    out: set[str] = set(tree.keys())
    for children in tree.values():
        out.update(children)
    return out


def expand_many(genres: Iterable[str]) -> set[str]:
    """Union of expand_branch over a list of genre names."""
    out: set[str] = set()
    for g in genres:
        out |= expand_branch(g)
    return out

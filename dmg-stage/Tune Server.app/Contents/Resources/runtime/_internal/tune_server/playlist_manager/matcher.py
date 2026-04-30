"""Track matching engine — multi-stage matching across streaming services.

Stages:
1. ISRC match (score 1.0, confidence high)
2. Exact metadata match (score 0.95, confidence high)
3. Fuzzy metadata match (score 0.5-0.9, confidence medium)
4. Broadened search (score 0.3-0.6, confidence low)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import structlog

logger = structlog.get_logger()

# Patterns to strip from track titles for comparison
_STRIP_PATTERNS = [
    r"\s*\(remaster(ed)?\s*\d*\)",
    r"\s*\[remaster(ed)?\s*\d*\]",
    r"\s*\(deluxe\s*(edition)?\)",
    r"\s*\[deluxe\s*(edition)?\]",
    r"\s*\(live\)",
    r"\s*\[live\]",
    r"\s*\(bonus\s*track\)",
    r"\s*\(feat\.?\s+[^)]+\)",
    r"\s*\[feat\.?\s+[^\]]+\]",
    r"\s*\(ft\.?\s+[^)]+\)",
    r"\s*-\s*remaster(ed)?\s*\d*",
    r"\s*\(mono\)",
    r"\s*\(stereo\)",
    r"\s*\(\d{4}\s*version\)",
]
_STRIP_RE = re.compile("|".join(_STRIP_PATTERNS), re.IGNORECASE)


@dataclass
class MatchCandidate:
    """A potential match for a source track on a target service."""
    title: str
    artist_name: str
    album_title: str = ""
    source_id: str = ""
    duration_ms: int = 0
    isrc: str = ""
    score: float = 0.0
    match_method: str = ""  # "isrc", "exact", "fuzzy", "broadened"
    confidence: str = ""    # "high", "medium", "low"


@dataclass
class MatchResult:
    """Result of matching a single source track."""
    source_title: str
    source_artist: str
    source_album: str = ""
    source_id: str = ""
    source_isrc: str = ""
    status: str = "not_found"  # "matched", "approximate", "not_found"
    best_match: MatchCandidate | None = None
    alternatives: list[MatchCandidate] = field(default_factory=list)


def normalize(text: str) -> str:
    """Normalize text for comparison: lowercase, strip accents, remove suffixes."""
    if not text:
        return ""
    # Unicode decompose + strip accents
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Lowercase
    result = ascii_text.lower().strip()
    # Strip common suffixes
    result = _STRIP_RE.sub("", result)
    # Normalize whitespace
    result = re.sub(r"\s+", " ", result).strip()
    return result


def normalize_artist(name: str) -> str:
    """Normalize artist name: strip 'The', handle VA."""
    n = normalize(name)
    if n.startswith("the "):
        n = n[4:]
    if n in ("various artists", "va", "various", "compilation"):
        n = "various artists"
    return n


def similarity(a: str, b: str) -> float:
    """Compute similarity ratio between two strings (0.0 to 1.0)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def duration_similarity(d1: int, d2: int) -> float:
    """Compare durations in ms. Returns 1.0 for exact, 0.0 for >30s diff."""
    if d1 <= 0 or d2 <= 0:
        return 0.5  # unknown duration, neutral score
    diff_ms = abs(d1 - d2)
    if diff_ms < 2000:  # <2s
        return 1.0
    if diff_ms > 30000:  # >30s
        return 0.0
    return 1.0 - (diff_ms / 30000)


def compute_score(
    source_title: str,
    source_artist: str,
    source_album: str,
    source_duration_ms: int,
    candidate_title: str,
    candidate_artist: str,
    candidate_album: str,
    candidate_duration_ms: int,
) -> float:
    """Compute composite match score (0.0 to 1.0)."""
    title_sim = similarity(normalize(source_title), normalize(candidate_title))
    artist_sim = similarity(normalize_artist(source_artist), normalize_artist(candidate_artist))
    album_sim = similarity(normalize(source_album), normalize(candidate_album))
    dur_sim = duration_similarity(source_duration_ms, candidate_duration_ms)

    # Weighted composite
    score = (title_sim * 0.4) + (artist_sim * 0.3) + (dur_sim * 0.2) + (album_sim * 0.1)
    return round(score, 3)


async def find_best_match(
    source_title: str,
    source_artist: str,
    source_album: str = "",
    source_duration_ms: int = 0,
    source_isrc: str = "",
    search_func=None,
    threshold: float = 0.6,
) -> MatchResult:
    """Find the best match for a source track using multi-stage matching.

    Args:
        source_title: Track title
        source_artist: Artist name
        source_album: Album title (optional)
        source_duration_ms: Duration in ms (optional, improves matching)
        source_isrc: ISRC code (optional, strongest match)
        search_func: async function(query: str, limit: int) -> list[dict]
            Each dict must have: title, artist_name, source_id
            Optional: album_title, duration_ms, isrc
        threshold: Minimum score to accept a match

    Returns:
        MatchResult with best match and alternatives
    """
    result = MatchResult(
        source_title=source_title,
        source_artist=source_artist,
        source_album=source_album,
        source_isrc=source_isrc,
    )

    if search_func is None:
        return result

    # Stage 1: ISRC match
    if source_isrc:
        try:
            candidates = await search_func(source_isrc, 5)
            for c in candidates:
                c_isrc = c.get("isrc", "")
                if c_isrc and c_isrc.upper() == source_isrc.upper():
                    match = MatchCandidate(
                        title=c.get("title", ""),
                        artist_name=c.get("artist_name", ""),
                        album_title=c.get("album_title", ""),
                        source_id=c.get("source_id", ""),
                        duration_ms=c.get("duration_ms", 0),
                        isrc=c_isrc,
                        score=1.0,
                        match_method="isrc",
                        confidence="high",
                    )
                    result.best_match = match
                    result.status = "matched"
                    return result
        except Exception:
            pass

    # Stage 2 & 3: Search by artist + title
    query = f"{source_artist} {source_title}"
    try:
        candidates = await search_func(query, 15)
    except Exception:
        logger.debug("match_search_error", query=query[:60])
        return result

    scored = []
    for c in candidates:
        c_title = c.get("title", "")
        c_artist = c.get("artist_name", "")
        c_album = c.get("album_title", "")
        c_duration = c.get("duration_ms", 0)

        score = compute_score(
            source_title, source_artist, source_album, source_duration_ms,
            c_title, c_artist, c_album, c_duration,
        )

        # Exact match bonus
        if normalize(source_title) == normalize(c_title) and \
           normalize_artist(source_artist) == normalize_artist(c_artist):
            score = max(score, 0.95)

        mc = MatchCandidate(
            title=c_title,
            artist_name=c_artist,
            album_title=c_album,
            source_id=c.get("source_id", ""),
            duration_ms=c_duration,
            isrc=c.get("isrc", ""),
            score=score,
            match_method="exact" if score >= 0.95 else "fuzzy",
            confidence="high" if score >= 0.9 else "medium" if score >= 0.7 else "low",
        )
        scored.append(mc)

    # Stage 4: Broadened search (title only) if no good match
    if not scored or max(s.score for s in scored) < threshold:
        try:
            broad_candidates = await search_func(source_title, 10)
            for c in broad_candidates:
                score = compute_score(
                    source_title, source_artist, source_album, source_duration_ms,
                    c.get("title", ""), c.get("artist_name", ""),
                    c.get("album_title", ""), c.get("duration_ms", 0),
                )
                mc = MatchCandidate(
                    title=c.get("title", ""),
                    artist_name=c.get("artist_name", ""),
                    album_title=c.get("album_title", ""),
                    source_id=c.get("source_id", ""),
                    duration_ms=c.get("duration_ms", 0),
                    score=score,
                    match_method="broadened",
                    confidence="low",
                )
                scored.append(mc)
        except Exception:
            pass

    # Sort by score descending, deduplicate by source_id
    seen = set()
    unique = []
    for mc in sorted(scored, key=lambda x: x.score, reverse=True):
        if mc.source_id not in seen:
            seen.add(mc.source_id)
            unique.append(mc)

    if unique and unique[0].score >= threshold:
        result.best_match = unique[0]
        result.status = "matched" if unique[0].score >= 0.9 else "approximate"
        result.alternatives = unique[1:5]  # top 4 alternatives
    elif unique:
        result.alternatives = unique[:5]

    return result

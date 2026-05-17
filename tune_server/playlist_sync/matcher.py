"""TrackMatcher — fuzzy matching of tracks across streaming services.

Uses title + artist fuzzy matching via difflib.SequenceMatcher, with
normalization to strip parenthetical suffixes (remaster, feat, live, etc.).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import structlog

logger = structlog.get_logger()

# Patterns stripped before comparison
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
class TrackInfo:
    """Minimal track representation for matching."""

    title: str
    artist_name: str
    album_title: str = ""
    source_id: str = ""
    duration_ms: int = 0
    isrc: str = ""
    source: str = ""


@dataclass
class MatchCandidate:
    """A potential match on the target service."""

    title: str
    artist_name: str
    album_title: str = ""
    source_id: str = ""
    duration_ms: int = 0
    score: float = 0.0
    confidence: str = ""  # "high", "medium", "low"


@dataclass
class TrackMatchResult:
    """Result of matching a single source track."""

    source: TrackInfo
    status: str = "not_found"  # "matched", "approximate", "not_found"
    best_match: MatchCandidate | None = None
    alternatives: list[MatchCandidate] = field(default_factory=list)


def _normalize(text: str) -> str:
    """Lowercase, strip accents, remove remaster/feat/live suffixes."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in nfkd if not unicodedata.combining(c))
    result = ascii_text.lower().strip()
    result = _STRIP_RE.sub("", result)
    result = re.sub(r"\s+", " ", result).strip()
    return result


def _normalize_artist(name: str) -> str:
    """Normalize artist: strip 'The', unify VA."""
    n = _normalize(name)
    if n.startswith("the "):
        n = n[4:]
    if n in ("various artists", "va", "various", "compilation"):
        n = "various artists"
    return n


def _similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio between two strings."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _duration_similarity(d1: int, d2: int) -> float:
    """Compare durations. 1.0 for exact, 0.0 for >30s diff."""
    if d1 <= 0 or d2 <= 0:
        return 0.5  # unknown
    diff_ms = abs(d1 - d2)
    if diff_ms < 2000:
        return 1.0
    if diff_ms > 30000:
        return 0.0
    return 1.0 - (diff_ms / 30000)


class TrackMatcher:
    """Matches tracks across services using fuzzy title+artist comparison."""

    def __init__(self, threshold: float = 0.6) -> None:
        self.threshold = threshold

    def compute_score(
        self,
        source: TrackInfo,
        candidate_title: str,
        candidate_artist: str,
        candidate_album: str = "",
        candidate_duration_ms: int = 0,
    ) -> float:
        """Compute composite match score (0.0 to 1.0)."""
        title_sim = _similarity(_normalize(source.title), _normalize(candidate_title))
        artist_sim = _similarity(
            _normalize_artist(source.artist_name),
            _normalize_artist(candidate_artist),
        )
        album_sim = _similarity(
            _normalize(source.album_title), _normalize(candidate_album)
        )
        dur_sim = _duration_similarity(source.duration_ms, candidate_duration_ms)

        # Weighted: title most important, then artist, duration, album
        score = (title_sim * 0.4) + (artist_sim * 0.3) + (dur_sim * 0.2) + (album_sim * 0.1)
        return round(score, 3)

    async def find_match(
        self,
        source: TrackInfo,
        search_func,
    ) -> TrackMatchResult:
        """Find best match for *source* using *search_func*.

        Args:
            source: The track to match.
            search_func: ``async (query: str, limit: int) -> SearchResult``
                where SearchResult has a ``.tracks`` list of Track objects
                (each with title, artist_name, source_id, album_title,
                duration_ms).

        Returns:
            TrackMatchResult with best_match and alternatives.
        """
        result = TrackMatchResult(source=source)

        if search_func is None:
            return result

        query = f"{source.artist_name} {source.title}"
        try:
            search_result = await search_func(query, 15)
        except Exception:
            logger.debug("track_matcher_search_error", query=query[:80])
            return result

        candidates = search_result.tracks if hasattr(search_result, "tracks") else search_result
        scored: list[MatchCandidate] = []

        for c in candidates:
            c_title = c.title if hasattr(c, "title") else c.get("title", "")
            c_artist = c.artist_name if hasattr(c, "artist_name") else c.get("artist_name", "")
            c_album = c.album_title if hasattr(c, "album_title") else c.get("album_title", "")
            c_dur = c.duration_ms if hasattr(c, "duration_ms") else c.get("duration_ms", 0)
            c_sid = c.source_id if hasattr(c, "source_id") else c.get("source_id", "")

            score = self.compute_score(source, c_title, c_artist, c_album, c_dur)

            # Exact metadata bonus
            if (
                _normalize(source.title) == _normalize(c_title)
                and _normalize_artist(source.artist_name) == _normalize_artist(c_artist)
            ):
                score = max(score, 0.95)

            confidence = "high" if score >= 0.9 else "medium" if score >= 0.7 else "low"
            scored.append(
                MatchCandidate(
                    title=c_title,
                    artist_name=c_artist,
                    album_title=c_album,
                    source_id=c_sid,
                    duration_ms=c_dur,
                    score=score,
                    confidence=confidence,
                )
            )

        # Broadened search (title only) if nothing good
        if not scored or max(s.score for s in scored) < self.threshold:
            try:
                broad_result = await search_func(source.title, 10)
                broad_candidates = (
                    broad_result.tracks
                    if hasattr(broad_result, "tracks")
                    else broad_result
                )
                for c in broad_candidates:
                    c_title = c.title if hasattr(c, "title") else c.get("title", "")
                    c_artist = c.artist_name if hasattr(c, "artist_name") else c.get("artist_name", "")
                    c_album = c.album_title if hasattr(c, "album_title") else c.get("album_title", "")
                    c_dur = c.duration_ms if hasattr(c, "duration_ms") else c.get("duration_ms", 0)
                    c_sid = c.source_id if hasattr(c, "source_id") else c.get("source_id", "")

                    score = self.compute_score(source, c_title, c_artist, c_album, c_dur)
                    scored.append(
                        MatchCandidate(
                            title=c_title,
                            artist_name=c_artist,
                            album_title=c_album,
                            source_id=c_sid,
                            duration_ms=c_dur,
                            score=score,
                            confidence="low",
                        )
                    )
            except Exception:
                pass

        # Sort descending, deduplicate by source_id
        seen: set[str] = set()
        unique: list[MatchCandidate] = []
        for mc in sorted(scored, key=lambda x: x.score, reverse=True):
            if mc.source_id and mc.source_id not in seen:
                seen.add(mc.source_id)
                unique.append(mc)

        if unique and unique[0].score >= self.threshold:
            result.best_match = unique[0]
            result.status = "matched" if unique[0].score >= 0.9 else "approximate"
            result.alternatives = unique[1:5]
        elif unique:
            result.alternatives = unique[:5]

        return result

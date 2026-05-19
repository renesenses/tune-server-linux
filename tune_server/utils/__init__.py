"""Tune Server shared utilities."""
from __future__ import annotations

import re
import unicodedata


def fold_accents(text: str) -> str:
    """Fold accented characters to their ASCII base form.

    Examples: carlão -> carlao, résumé -> resume, Björk -> Bjork.
    """
    if not text:
        return text
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


_FTS5_SPECIAL = re.compile(r'[()":+\-^{}]')
_FTS5_KEYWORDS = re.compile(r'\b(?:AND|OR|NOT|NEAR)\b', re.IGNORECASE)


def sanitize_fts_query(query: str) -> str:
    """Strip FTS5 special syntax from a user query to prevent parse errors.

    Parentheses, quotes, and boolean keywords (AND/OR/NOT/NEAR) are
    removed so the string can be safely passed to FTS5 MATCH.
    """
    safe = _FTS5_SPECIAL.sub(" ", query)
    safe = _FTS5_KEYWORDS.sub(" ", safe)
    return " ".join(safe.split())

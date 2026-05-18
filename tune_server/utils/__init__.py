"""Tune Server shared utilities."""
from __future__ import annotations

import unicodedata


def fold_accents(text: str) -> str:
    """Fold accented characters to their ASCII base form.

    Examples: carlão -> carlao, résumé -> resume, Björk -> Bjork.
    """
    if not text:
        return text
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))

"""Rust-accelerated library scanner (Phase 4 migration).

When tune_native is available, provides parallel file walking and
metadata reading via rayon (all CPU cores), replacing the sequential
Python scanner for the I/O-heavy scan phase.

The Python LibraryScanner keeps DB operations and album splitting logic;
the Rust layer handles file enumeration, metadata reading, and audio hashing.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()

try:
    import tune_native

    _RUST_SCANNER_AVAILABLE = (
        hasattr(tune_native, "scan_directories")
        and hasattr(tune_native, "list_audio_files")
        and hasattr(tune_native, "audio_hash")
    )
except ImportError:
    _RUST_SCANNER_AVAILABLE = False


def rust_scanner_available() -> bool:
    from tune_server.config import settings
    engine = settings.scanner_engine
    return _RUST_SCANNER_AVAILABLE and engine != "python"


def list_audio_files(dirs: list[str]) -> list[str]:
    """List audio files using Rust walkdir (faster than Python os.walk)."""
    if not _RUST_SCANNER_AVAILABLE:
        raise RuntimeError("Rust scanner not available")
    return list(tune_native.list_audio_files(dirs))


def scan_directories(dirs: list[str], with_hash: bool = False) -> dict:
    """Parallel scan using Rust rayon — reads metadata on all CPU cores.

    Returns dict with 'files' (list of dicts) and 'stats'.
    Each file dict has: path, file_size, mtime, metadata (optional), audio_hash (optional).
    """
    if not _RUST_SCANNER_AVAILABLE:
        raise RuntimeError("Rust scanner not available")
    return tune_native.scan_directories(dirs, with_hash=with_hash)


def compute_audio_hash(path: str) -> str | None:
    """Compute audio hash using Rust MD5 (64KB at 25% offset)."""
    if not _RUST_SCANNER_AVAILABLE:
        raise RuntimeError("Rust scanner not available")
    return tune_native.audio_hash(path)


def same_quality_tier(sr1: int | None, sr2: int | None) -> bool:
    """Check if two sample rates are in the same quality tier."""
    if not _RUST_SCANNER_AVAILABLE:
        return sr1 == sr2 if sr1 is not None and sr2 is not None else True
    return tune_native.same_quality_tier(sr1, sr2)


def quality_suffix(sample_rate: int | None, bit_depth: int | None) -> str:
    """Generate quality suffix like '96kHz/24bit'."""
    if not _RUST_SCANNER_AVAILABLE:
        parts = []
        if sample_rate and sample_rate > 44100:
            parts.append(f"{sample_rate // 1000}kHz")
        if bit_depth and bit_depth > 16:
            parts.append(f"{bit_depth}bit")
        return "/".join(parts)
    return tune_native.quality_suffix_fn(sample_rate, bit_depth)

"""Credentials vault for network share authentication.

Encrypts passwords at rest using Fernet symmetric encryption.
The encryption key is derived from a persistent secret stored in
``~/.tune/.credentials_key``.  If the ``cryptography`` package is not
installed, passwords are stored as base64-encoded plaintext (a
warning is logged on first use).
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import structlog

logger = structlog.get_logger()

_KEY_FILE = Path.home() / ".tune" / ".credentials_key"

# Lazy singletons
_fernet_instance = None
_fernet_available: bool | None = None


def _ensure_key_file() -> bytes:
    """Return the 32-byte master key, creating one if absent."""
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _KEY_FILE.exists():
        raw = _KEY_FILE.read_bytes().strip()
        if len(raw) >= 32:
            return raw[:32]
    # Generate a fresh 32-byte key
    raw = os.urandom(32)
    _KEY_FILE.write_bytes(raw)
    # Restrict permissions on Unix
    try:
        _KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return raw


def _get_fernet():
    """Return a Fernet instance (cached), or None if unavailable."""
    global _fernet_instance, _fernet_available
    if _fernet_available is False:
        return None
    if _fernet_instance is not None:
        return _fernet_instance
    try:
        from cryptography.fernet import Fernet

        raw_key = _ensure_key_file()
        # Fernet requires a url-safe base64-encoded 32-byte key
        fernet_key = base64.urlsafe_b64encode(raw_key)
        _fernet_instance = Fernet(fernet_key)
        _fernet_available = True
        return _fernet_instance
    except ImportError:
        _fernet_available = False
        logger.warning(
            "cryptography_not_installed",
            hint="pip install cryptography — passwords will be stored as base64 (not encrypted)",
        )
        return None


def encrypt_password(plaintext: str) -> str:
    """Encrypt a password for storage.

    Returns a string prefixed with ``fernet:`` (encrypted) or ``b64:``
    (base64 fallback).
    """
    if not plaintext:
        return ""
    fernet = _get_fernet()
    if fernet:
        token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return f"fernet:{token}"
    # Fallback: base64-only (NOT secure — but functional without cryptography)
    encoded = base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
    return f"b64:{encoded}"


def decrypt_password(stored: str) -> str:
    """Decrypt a stored password string.

    Handles ``fernet:`` and ``b64:`` prefixes.  Plain strings (legacy
    rows written before encryption was added) are returned as-is.
    """
    if not stored:
        return ""
    if stored.startswith("fernet:"):
        token = stored[7:]
        fernet = _get_fernet()
        if fernet:
            try:
                return fernet.decrypt(token.encode("ascii")).decode("utf-8")
            except Exception:
                logger.warning("credential_decrypt_failed", hint="key may have changed")
                return ""
        # cryptography not installed but data was encrypted — cannot decrypt
        logger.warning("credential_encrypted_but_no_cryptography")
        return ""
    if stored.startswith("b64:"):
        encoded = stored[4:]
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except Exception:
            return ""
    # Legacy plain-text password (pre-vault)
    return stored

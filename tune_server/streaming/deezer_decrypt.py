"""Deezer Blowfish CBC stripe decryption.

Deezer encrypts audio streams with Blowfish-CBC. The cipher is applied in
"stripe" mode: chunks of 2048 bytes, only the first of every three is
encrypted, the next two pass through. The per-track key is derived from
the SNG_ID via an XOR-with-secret scheme.

Reference: librespot-deezer / deemix / d-fi cloning the Deezer player
behaviour. The secret string is documented across multiple open-source
implementations and is the same value as the official desktop client.
"""
from __future__ import annotations

import hashlib
from typing import AsyncIterator, Iterable

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Constants from the Deezer client. Public in every open-source clone.
_SECRET = b"g4el58wc0zvf9na1"
_IV = bytes([0, 1, 2, 3, 4, 5, 6, 7])
_CHUNK = 2048


def compute_blowfish_key(sng_id: str | int) -> bytes:
    """Derive the per-track Blowfish key from the SNG_ID."""
    md5 = hashlib.md5(str(sng_id).encode("ascii")).hexdigest().encode("ascii")
    key = bytes(
        md5[i] ^ md5[i + 16] ^ _SECRET[i] for i in range(16)
    )
    return key


def _decryptor(key: bytes):
    return Cipher(algorithms.Blowfish(key), modes.CBC(_IV), backend=default_backend()).decryptor()


def decrypt_chunk(chunk: bytes, key: bytes) -> bytes:
    """Decrypt a single 2048-byte chunk. Caller is responsible for stripe logic."""
    if len(chunk) < 8:
        return chunk  # Below Blowfish block size — pass through (tail)
    # Truncate to multiple of block size (8) for CBC, then re-append remainder.
    aligned_len = (len(chunk) // 8) * 8
    head, tail = chunk[:aligned_len], chunk[aligned_len:]
    dec = _decryptor(key)
    return dec.update(head) + dec.finalize() + tail


async def decrypt_stream(
    source: AsyncIterator[bytes], sng_id: str | int,
) -> AsyncIterator[bytes]:
    """Async generator: consume `source` (raw encrypted bytes), yield plain bytes.

    Every 2048-byte chunk's stripe position alternates 0,1,2,0,1,2,... and
    only position-0 chunks are decrypted. Smaller terminal chunks (<2048
    bytes) are passed through unchanged — Deezer pads streams so the final
    chunk is always plain.
    """
    key = compute_blowfish_key(sng_id)
    buffer = bytearray()
    chunk_index = 0
    async for piece in source:
        if not piece:
            continue
        buffer.extend(piece)
        while len(buffer) >= _CHUNK:
            chunk = bytes(buffer[:_CHUNK])
            del buffer[:_CHUNK]
            if chunk_index % 3 == 0:
                yield decrypt_chunk(chunk, key)
            else:
                yield chunk
            chunk_index += 1
    if buffer:
        # Tail chunk smaller than 2048 — Deezer leaves it plain.
        yield bytes(buffer)


def decrypt_iterable(source: Iterable[bytes], sng_id: str | int) -> Iterable[bytes]:
    """Sync variant of decrypt_stream — useful for tests."""
    key = compute_blowfish_key(sng_id)
    buffer = bytearray()
    chunk_index = 0
    for piece in source:
        if not piece:
            continue
        buffer.extend(piece)
        while len(buffer) >= _CHUNK:
            chunk = bytes(buffer[:_CHUNK])
            del buffer[:_CHUNK]
            if chunk_index % 3 == 0:
                yield decrypt_chunk(chunk, key)
            else:
                yield chunk
            chunk_index += 1
    if buffer:
        yield bytes(buffer)

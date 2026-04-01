from __future__ import annotations

import asyncio
from collections import deque


class AsyncRingBuffer:
    """Async-safe bounded buffer for audio chunks."""

    def __init__(self, max_chunks: int = 64) -> None:
        self._max = max_chunks
        self._buffer: deque[bytes] = deque(maxlen=max_chunks)
        self._not_empty = asyncio.Event()
        self._not_full = asyncio.Event()
        self._not_full.set()
        self._closed = False

    @property
    def size(self) -> int:
        return len(self._buffer)

    @property
    def is_full(self) -> bool:
        return len(self._buffer) >= self._max

    @property
    def is_empty(self) -> bool:
        return len(self._buffer) == 0

    async def put(self, chunk: bytes) -> None:
        while self.is_full and not self._closed:
            self._not_full.clear()
            await self._not_full.wait()

        if self._closed:
            return

        self._buffer.append(chunk)
        self._not_empty.set()

    async def get(self) -> bytes | None:
        while self.is_empty and not self._closed:
            self._not_empty.clear()
            await self._not_empty.wait()

        if self.is_empty and self._closed:
            return None

        chunk = self._buffer.popleft()
        self._not_full.set()
        return chunk

    def close(self) -> None:
        self._closed = True
        self._not_empty.set()
        self._not_full.set()

    def reset(self) -> None:
        self._buffer.clear()
        self._closed = False
        self._not_full.set()
        self._not_empty.clear()

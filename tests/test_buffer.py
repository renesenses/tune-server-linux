from __future__ import annotations

import asyncio

import pytest

from tune_server.audio.buffer import AsyncRingBuffer


class TestPutGet:
    async def test_put_get(self):
        buf = AsyncRingBuffer(max_chunks=8)
        await buf.put(b"hello")
        chunk = await buf.get()
        assert chunk == b"hello"

    async def test_ordering_fifo(self):
        buf = AsyncRingBuffer(max_chunks=8)
        await buf.put(b"A")
        await buf.put(b"B")
        assert await buf.get() == b"A"
        assert await buf.get() == b"B"


class TestSizeTracking:
    async def test_size_increments_decrements(self):
        buf = AsyncRingBuffer(max_chunks=8)
        assert buf.size == 0
        await buf.put(b"X")
        assert buf.size == 1
        await buf.put(b"Y")
        assert buf.size == 2
        await buf.get()
        assert buf.size == 1


class TestIsEmptyIsFull:
    async def test_properties(self):
        buf = AsyncRingBuffer(max_chunks=2)
        assert buf.is_empty is True
        assert buf.is_full is False

        await buf.put(b"A")
        assert buf.is_empty is False
        assert buf.is_full is False

        await buf.put(b"B")
        assert buf.is_full is True


class TestClose:
    async def test_close_unblocks_get(self):
        buf = AsyncRingBuffer(max_chunks=8)

        async def delayed_close():
            await asyncio.sleep(0.05)
            buf.close()

        asyncio.create_task(delayed_close())
        result = await asyncio.wait_for(buf.get(), timeout=1.0)
        assert result is None

    async def test_close_unblocks_put(self):
        buf = AsyncRingBuffer(max_chunks=1)
        await buf.put(b"X")  # now full

        async def delayed_close():
            await asyncio.sleep(0.05)
            buf.close()

        asyncio.create_task(delayed_close())
        # put() should return without blocking forever
        await asyncio.wait_for(buf.put(b"Y"), timeout=1.0)

    async def test_get_after_close_drains(self):
        buf = AsyncRingBuffer(max_chunks=8)
        await buf.put(b"A")
        await buf.put(b"B")
        buf.close()

        # Should still get remaining items
        assert await buf.get() == b"A"
        assert await buf.get() == b"B"
        # Then None
        assert await buf.get() is None


class TestBackpressure:
    async def test_backpressure(self):
        buf = AsyncRingBuffer(max_chunks=2)
        await buf.put(b"A")
        await buf.put(b"B")
        # Buffer is full now

        put_done = asyncio.Event()

        async def delayed_put():
            await buf.put(b"C")
            put_done.set()

        task = asyncio.create_task(delayed_put())

        # Give the put a chance to block
        await asyncio.sleep(0.05)
        assert not put_done.is_set()

        # Consume one item to unblock
        await buf.get()
        await asyncio.wait_for(put_done.wait(), timeout=1.0)
        assert put_done.is_set()
        task.cancel()


class TestReset:
    async def test_reset(self):
        buf = AsyncRingBuffer(max_chunks=8)
        await buf.put(b"X")
        buf.close()
        buf.reset()

        assert buf.is_empty is True
        assert buf.size == 0
        # Should be usable again
        await buf.put(b"Y")
        assert await buf.get() == b"Y"


class TestConcurrency:
    async def test_concurrent_producers_consumers(self):
        buf = AsyncRingBuffer(max_chunks=4)
        produced = []
        consumed = []

        async def producer(n: int, count: int):
            for i in range(count):
                data = f"p{n}:{i}".encode()
                await buf.put(data)
                produced.append(data)

        async def consumer(count: int):
            for _ in range(count):
                chunk = await buf.get()
                if chunk is None:
                    break
                consumed.append(chunk)

        total = 20
        # 2 producers × 10 items each
        await asyncio.gather(
            producer(0, total // 2),
            producer(1, total // 2),
            consumer(total),
        )

        assert len(consumed) == total
        assert set(consumed) == set(produced)

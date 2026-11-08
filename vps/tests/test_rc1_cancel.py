"""Cancellation during cache staging leaves no reusable incomplete entry."""
import asyncio

import pytest

from r2relay_vps.cache import CacheStore


@pytest.mark.asyncio
async def test_cancel_during_stream_never_publishes_partial_part(tmp_path):
    cache = CacheStore(tmp_path)
    key = "f" * 32
    started = asyncio.Event()
    never = asyncio.Event()

    async def interrupted_stream():
        yield b"partial"
        started.set()
        await never.wait()
        yield b"remaining"

    task = asyncio.create_task(cache.put(key, interrupted_stream(), 16))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cache.get(key) is None
    assert not list(tmp_path.glob("*/*.part"))
    assert not list(tmp_path.glob("*/*.tmp"))

    async def complete_stream():
        yield b"partialremaining"

    entry = await cache.put(key, complete_stream(), 16)
    assert entry.path.read_bytes() == b"partialremaining"

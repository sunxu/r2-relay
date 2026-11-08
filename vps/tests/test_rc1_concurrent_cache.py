"""RC1 concurrency regression: first MD5 writer keeps its fixed lifetime."""
import asyncio

import pytest

from r2relay_vps.cache import CacheStore


@pytest.mark.asyncio
async def test_concurrent_miss_same_md5_never_overwrites_first_winner(tmp_path):
    now = [1000.0]
    store = CacheStore(tmp_path, clock=lambda: now[0])
    reached = 0
    ready = asyncio.Event()

    async def chunks():
        nonlocal reached
        yield b"first"
        reached += 1
        if reached == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=5)
        yield b"writer"

    key = "b" * 32
    results = await asyncio.gather(
        store.put(key, chunks(), 11),
        store.put(key, chunks(), 11),
    )
    assert results[0].created_at == results[1].created_at == 1000.0
    assert store.get(key).path.read_bytes() == b"firstwriter"
    assert len(list(tmp_path.glob("*/*.part"))) == 1
    assert not list(tmp_path.glob("*/*.tmp"))
    original_mtime = store.get(key).path.stat().st_mtime

    now[0] = 1400.0

    async def repeat():
        yield b"firstwriter"

    assert (await store.put(key, repeat(), 11)).created_at == 1000.0
    assert store.get(key).path.stat().st_mtime == original_mtime
    recovered = CacheStore(tmp_path, clock=lambda: now[0])
    assert recovered.get(key).expires_at == 2800.0
    now[0] = 2800.0
    assert recovered.get(key) is None


@pytest.mark.asyncio
async def test_hit_still_consumes_complete_body(tmp_path):
    store = CacheStore(tmp_path)
    key = "c" * 32

    async def complete():
        yield b"abc"

    await store.put(key, complete(), 3)
    assert store.get(key) is not None

    async def truncated():
        yield b"ab"

    with pytest.raises(ValueError, match="length mismatch"):
        await store.put(key, truncated(), 3)

    async def excessive():
        yield b"abcd"

    with pytest.raises(ValueError, match="exceeds"):
        await store.put(key, excessive(), 3)
    assert store.get(key).path.read_bytes() == b"abc"

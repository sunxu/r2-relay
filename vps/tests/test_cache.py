import hashlib
import os
from pathlib import Path

import pytest
from r2relay_vps.cache import CacheStore


async def chunks(*values):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_put_hit_restart_and_fixed_expiry(tmp_path):
    now = [10000.0]
    key = hashlib.md5(b"hello").hexdigest()
    cache = CacheStore(tmp_path, clock=lambda: now[0])
    entry = await cache.put(key, chunks(b"hel", b"lo"), 5)
    assert entry.path.read_bytes() == b"hello"
    assert entry.created_at == 10000
    now[0] += 900
    assert cache.get(key) is not None
    await cache.put(key, chunks(b"hello"), 5)
    assert cache.get(key).expires_at == 11800
    recovered = CacheStore(tmp_path, clock=lambda: now[0])
    assert recovered.get(key) is not None
    now[0] = 11800
    assert recovered.get(key) is None
    assert not entry.path.exists()


@pytest.mark.asyncio
async def test_reject_incomplete_body(tmp_path):
    cache = CacheStore(tmp_path)
    with pytest.raises(ValueError, match="length"):
        await cache.put("a" * 32, chunks(b"abc"), 4)
    assert cache.get("a" * 32) is None
    assert not list(tmp_path.glob("*/*.tmp"))


@pytest.mark.asyncio
async def test_reject_invalid_md5(tmp_path):
    cache = CacheStore(tmp_path)
    with pytest.raises(ValueError, match="md5"):
        await cache.put("../outside", chunks(b"x"), 1)


def test_cleanup_rebuild_removes_expired_files(tmp_path):
    path = tmp_path / "ab" / ("ab" * 16 + ".part")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"cached")
    os.utime(path, (10, 10))
    store = CacheStore(tmp_path, clock=lambda: 2000)
    assert len(store.entries) == 0
    assert not path.exists()

from types import SimpleNamespace

import pytest

from r2relay_vps.cache import CacheStore


async def content():
    yield b"part"


@pytest.mark.asyncio
async def test_cache_low_disk_space_rejected_before_writing(tmp_path, monkeypatch):
    store = CacheStore(tmp_path)
    monkeypatch.setenv("R2_RELAY_MIN_FREE_BYTES", "100")
    monkeypatch.setattr(
        "r2relay_vps.cache.shutil.disk_usage",
        lambda path: SimpleNamespace(free=102),
    )
    with pytest.raises(OSError, match="disk space"):
        await store.put("a" * 32, content(), 4)
    assert store.get("a" * 32) is None
    assert not list(tmp_path.glob("*/*.tmp"))

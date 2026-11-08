"""VPS storage-full errors must be safely distinguishable from uncertain PUT state."""
import hashlib

import httpx
import pytest

from r2relay_local.relay import RelayClient, RelayStorageUnavailable
from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore


@pytest.mark.asyncio
async def test_storage_full_returns_507_no_r2_put_and_allows_direct_fallback(tmp_path, monkeypatch):
    cache = CacheStore(tmp_path)
    r2_calls = []

    async def fail_storage(md5, chunks, expected_size):
        raise OSError("simulated ENOSPC")

    monkeypatch.setattr(cache, "put", fail_storage)
    app = create_app(
        cache, token="test",
        r2_transport=httpx.MockTransport(
            lambda req: (r2_calls.append(req) or httpx.Response(200))
        ),
    )
    body = b"cache full scenario"
    digest = hashlib.md5(body).hexdigest()
    auth = {"Authorization": "Bearer test"}
    payload = {
        "request_id": "storagefull", "md5": digest, "size": len(body),
        "r2_request": {
            "method": "PUT",
            "url": "https://abc.r2.cloudflarestorage.com/b/k?uploadId=x&partNumber=1",
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as connection:
        relay = RelayClient("https://test", "test", client=connection)
        with pytest.raises(RelayStorageUnavailable):
            await relay.upload(payload["r2_request"]["url"], [], body)
        assert r2_calls == []
        assert not cache.entries
        executions = list(app.state.relay.executions.values())
        assert len(executions) == 1
        assert executions[0]["status"] == "FAILED"
        assert executions[0]["relay_error"]["type"] == "CacheStorageUnavailable"

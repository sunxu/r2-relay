import hashlib

import httpx
import pytest

from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore


@pytest.mark.asyncio
async def test_vps_execution_capacity_returns_503_without_creating_more(tmp_path, monkeypatch):
    monkeypatch.setenv("R2_RELAY_MAX_EXECUTIONS", "1")
    app = create_app(CacheStore(tmp_path), token="test")
    payload = {
        "request_id": "one", "md5": hashlib.md5(b"abc").hexdigest(), "size": 3,
        "r2_request": {"method": "PUT",
            "url": "https://abc.r2.cloudflarestorage.com/b/k?uploadId=a&partNumber=1"}
    }
    auth = {"Authorization": "Bearer test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post("/v1/parts/prepare", headers=auth, json=payload)
        assert first.status_code == 200
        second = await client.post("/v1/parts/prepare", headers=auth, json=payload)
        assert second.status_code == 503
        assert len(app.state.relay.executions) == 1
        assert len(app.state.relay.transfers) == 1


def test_stale_transfer_is_failed_and_record_eventually_removed(tmp_path, monkeypatch):
    now = [1000]
    monkeypatch.setenv("R2_RELAY_EXECUTION_TTL", "10")
    app = create_app(CacheStore(tmp_path), token="test")
    state = app.state.relay
    state.transfers["test"] = ("e1", None, now[0])
    state.executions["e1"] = {"status": "QUEUED", "created_at": now[0]}
    monkeypatch.setattr("r2relay_vps.app.time.time", lambda: 1011)
    state.prune()
    assert "test" not in state.transfers
    assert state.executions["e1"]["status"] == "FAILED"
    monkeypatch.setattr("r2relay_vps.app.time.time", lambda: 1022)
    state.prune()
    assert "e1" not in state.executions

"""Retained R2 response memory is separately capped and reclaimed."""
import asyncio
import hashlib

import httpx
import pytest

from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore


@pytest.mark.asyncio
async def test_r2_response_retention_budget_fails_closed_and_reclaims(tmp_path, monkeypatch):
    monkeypatch.setenv("R2_RELAY_MAX_RETAINED_RESULT_BYTES", "12")
    monkeypatch.setenv("R2_RELAY_EXECUTION_TTL", "1")
    app = create_app(CacheStore(tmp_path), token="test",
                     r2_transport=httpx.MockTransport(
                         lambda _: httpx.Response(200, content=b"x" * 16)
                     ), retry_delays=(0, 0, 0))
    data = b"part"
    digest = hashlib.md5(data).hexdigest()
    auth = {"Authorization": "Bearer test"}
    payload = {
        "request_id": "mem", "md5": digest, "size": len(data),
        "r2_request": {"method": "PUT",
                       "url": "https://a.r2.cloudflarestorage.com/b/k?uploadId=x&partNumber=1"},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        p = (await client.post("/v1/parts/prepare", json=payload, headers=auth)).json()
        await client.put("/v1/parts/data/" + p["transfer_id"], content=data,
                         headers={**auth, "X-Content-MD5-Hex": digest})
        for _ in range(100):
            current = (await client.get(
                "/v1/executions/" + p["execution_id"], headers=auth
            )).json()
            if current["status"] in ("FAILED", "SUCCEEDED"):
                break
            await asyncio.sleep(0.01)
        metrics = (await client.get("/v1/metrics", headers=auth)).json()
    assert current["status"] == "FAILED"
    assert current["relay_error"]["type"] == "ResultMemoryBudgetExceeded"
    assert "upstream_response" not in current
    assert metrics["retained_result_bytes"] == 0


@pytest.mark.asyncio
async def test_retain_and_prune_response_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("R2_RELAY_MAX_RETAINED_RESULT_BYTES", "10000")
    clock = [1000.0]
    monkeypatch.setattr("r2relay_vps.app.time.time", lambda: clock[0])
    app = create_app(CacheStore(tmp_path), token="test",
                     r2_transport=httpx.MockTransport(
                         lambda _: httpx.Response(403, content=b"retained")
                     ), retry_delays=(0, 0, 0))
    data = b"part"
    digest = hashlib.md5(data).hexdigest()
    auth = {"Authorization": "Bearer test"}
    payload = {
        "request_id": "mem", "md5": digest, "size": len(data),
        "r2_request": {"method": "PUT",
                       "url": "https://a.r2.cloudflarestorage.com/b/k?uploadId=x&partNumber=1"},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        p = (await client.post("/v1/parts/prepare", json=payload, headers=auth)).json()
        await client.put("/v1/parts/data/" + p["transfer_id"], content=data,
                         headers={**auth, "X-Content-MD5-Hex": digest})
        for _ in range(100):
            result = (await client.get(
                "/v1/executions/" + p["execution_id"], headers=auth
            )).json()
            if result["status"] == "FAILED":
                break
            await asyncio.sleep(0.01)
        state = app.state.relay
        assert state.retained_result_bytes >= len(b"retained")
        clock[0] += state.execution_ttl + 1
        state.prune()
        assert state.retained_result_bytes == 0
        assert state.executions == {}

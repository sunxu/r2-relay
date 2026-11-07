"""Structured metrics are read-only and require authentication."""
import asyncio
import hashlib

import httpx
import pytest

from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore


@pytest.mark.asyncio
async def test_metrics_track_miss_hit_staged_bytes_and_r2_attempts(tmp_path):
    body = b"metrics"
    app = create_app(
        CacheStore(tmp_path), token="test",
        r2_transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"ETag": '"ok"'})
        ),
        retry_delays=(0, 0, 0),
    )
    auth = {"Authorization": "Bearer test"}
    url = "https://abc.r2.cloudflarestorage.com/b/k?uploadId=one&partNumber=1"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/metrics")).status_code == 401
        for i in range(2):
            p = {"request_id": str(i), "md5": hashlib.md5(body).hexdigest(),
                 "size": len(body),
                 "r2_request": {"method": "PUT", "url": url.replace("one", f"run{i}")}}
            response = (await client.post(
                "/v1/parts/prepare", headers=auth, json=p
            )).json()
            assert response["status"] == ("MISS" if i == 0 else "HIT")
            if i == 0:
                staged = await client.put(
                    "/v1/parts/data/" + response["transfer_id"], content=body,
                    headers={**auth, "X-Content-MD5-Hex": p["md5"]},
                )
                assert staged.status_code == 200
            for _ in range(100):
                result = (await client.get(
                    "/v1/executions/" + response["execution_id"], headers=auth
                )).json()
                if result["status"] == "SUCCEEDED":
                    break
                await asyncio.sleep(0.01)
            assert result["status"] == "SUCCEEDED"
        stats = (await client.get("/v1/metrics", headers=auth)).json()
    assert stats["prepare_hit"] == 1
    assert stats["prepare_miss"] == 1
    assert stats["staged_bytes"] == len(body)
    assert stats["r2_success"] == 2
    assert stats["r2_attempts"] == 2
    assert stats["cache_entry_count"] == 1

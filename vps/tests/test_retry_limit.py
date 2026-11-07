import asyncio
import hashlib

import httpx
import pytest

from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore


@pytest.mark.asyncio
async def test_max_three_extra_retries_and_fixed_content_length(tmp_path):
    calls = []

    def mock_r2(request):
        calls.append({"length": request.headers.get("content-length"),
                      "body": request.content})
        return httpx.Response(502, text="bad gateway")

    app = create_app(CacheStore(tmp_path), token="test",
                     r2_transport=httpx.MockTransport(mock_r2),
                     retry_delays=(0, 0, 0))
    body = b"x" * 8192
    digest = hashlib.md5(body).hexdigest()
    auth = {"Authorization": "Bearer test"}
    payload = {"request_id": "retry-limit", "md5": digest, "size": len(body),
               "r2_request": {"method": "PUT",
                 "url": "https://abc.r2.cloudflarestorage.com/b/k?uploadId=id&partNumber=1",
                 "headers": [["Content-Type", "application/octet-stream"]]}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        p = (await client.post("/v1/parts/prepare", json=payload, headers=auth)).json()
        r = await client.put(f"/v1/parts/data/{p['transfer_id']}", content=body,
                             headers={**auth, "X-Content-MD5-Hex": digest})
        assert r.status_code == 200
        for _ in range(100):
            state = (await client.get(f"/v1/executions/{p['execution_id']}",
                                      headers=auth)).json()
            if state["status"] == "FAILED":
                break
            await asyncio.sleep(0.01)
        assert state["status"] == "FAILED"
        assert state["attempts"] == 4
        assert state["upstream_response"]["status_code"] == 502
        assert len(calls) == 4
        assert all(c["length"] == str(len(body)) and c["body"] == body for c in calls)

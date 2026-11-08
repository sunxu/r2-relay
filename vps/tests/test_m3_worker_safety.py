"""M3: response caps and S3 SigV4 request preservation."""
import asyncio
import hashlib

import httpx
import pytest

from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore


@pytest.mark.asyncio
async def test_r2_response_over_eight_mib_fails_without_truncated_upstream(tmp_path):
    r2_called = []

    def handler(request):
        r2_called.append(request)
        return httpx.Response(403, content=b"E" * (8 * 1024 * 1024 + 1))

    app = create_app(
        CacheStore(tmp_path), token="test",
        r2_transport=httpx.MockTransport(handler), retry_delays=(0, 0, 0)
    )
    body = b"test"
    digest = hashlib.md5(body).hexdigest()
    headers = {"Authorization": "Bearer test"}
    payload = {
        "request_id": "oversize", "md5": digest, "size": len(body),
        "r2_request": {"method": "PUT", "url":
            "https://acct.r2.cloudflarestorage.com/b/k?uploadId=xx&partNumber=1"}
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://vps"
    ) as client:
        p = (await client.post("/v1/parts/prepare", headers=headers, json=payload)).json()
        assert (await client.put("/v1/parts/data/" + p["transfer_id"],
                   headers={**headers, "X-Content-MD5-Hex": digest},
                   content=body)).status_code == 200
        for _ in range(150):
            state = (await client.get(
                "/v1/executions/" + p["execution_id"], headers=headers
            )).json()
            if state["status"] == "FAILED":
                break
            await asyncio.sleep(0.01)
    assert state["status"] == "FAILED"
    assert state["relay_error"]["type"] == "ResponseTooLarge"
    assert "upstream_response" not in state
    assert len(r2_called) == 1


@pytest.mark.asyncio
async def test_presigned_path_query_and_host_not_modified(tmp_path):
    full_url = (
        "https://acct.r2.cloudflarestorage.com/bucket/a%2Fb%20c.bin?"
        "uploadId=A%2FB%2BC&partNumber=10"
        "&X-Amz-SignedHeaders=host%3Bcontent-type"
        "&X-Amz-Signature=deadbeef0123456789"
    )
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"ETag": '"valid"'})

    body = b"same bytes"
    digest = hashlib.md5(body).hexdigest()
    app = create_app(
        CacheStore(tmp_path), token="test",
        r2_transport=httpx.MockTransport(handler), retry_delays=(0, 0, 0)
    )
    headers = {"Authorization": "Bearer test"}
    payload = {"request_id": "signed", "md5": digest, "size": len(body),
               "r2_request": {
                   "method": "PUT", "url": full_url,
                   "headers": [
                       ["Host", "acct.r2.cloudflarestorage.com"],
                       ["Content-Type", "application/octet-stream"],
                   ],
               }}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://vps"
    ) as client:
        p = (await client.post("/v1/parts/prepare", headers=headers, json=payload)).json()
        await client.put("/v1/parts/data/" + p["transfer_id"], content=body,
                         headers={**headers, "X-Content-MD5-Hex": digest})
        for _ in range(150):
            state = (await client.get(
                "/v1/executions/" + p["execution_id"], headers=headers
            )).json()
            if state["status"] == "SUCCEEDED":
                break
            await asyncio.sleep(0.01)
    assert state["status"] == "SUCCEEDED"
    assert len(seen) == 1
    assert str(seen[0].url) == full_url
    assert seen[0].headers["Host"] == "acct.r2.cloudflarestorage.com"
    assert seen[0].headers["Content-Type"] == "application/octet-stream"
    assert seen[0].headers["Content-Length"] == str(len(body))

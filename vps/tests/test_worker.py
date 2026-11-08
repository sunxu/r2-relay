import asyncio
import base64
import hashlib

import httpx
import pytest

from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore


async def wait(client, eid, headers):
    for _ in range(100):
        r = (await client.get(f"/v1/executions/{eid}", headers=headers)).json()
        if r["status"] in ("SUCCEEDED", "FAILED"):
            return r
        await asyncio.sleep(0.01)
    raise AssertionError("R2 worker did not finish")


@pytest.mark.asyncio
async def test_cache_miss_then_real_r2_retry_and_etag(tmp_path):
    calls = []

    def mock_r2(request: httpx.Request):
        calls.append((request.url, request.content))
        if len(calls) == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, headers={"ETag": '"valid-md5"'})

    app = create_app(CacheStore(tmp_path), token="test",
                     r2_transport=httpx.MockTransport(mock_r2), retry_delays=(0, 0, 0))
    headers = {"Authorization": "Bearer test"}
    data = b"identical content"
    digest = hashlib.md5(data).hexdigest()
    payload = {"request_id": "a", "md5": digest, "size": len(data),
               "r2_request": {"method": "PUT",
                              "url": "https://abc.r2.cloudflarestorage.com/bucket/key?uploadId=xyz&partNumber=10",
                              "headers": [["Content-Type", "application/octet-stream"]]}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        prep = (await client.post("/v1/parts/prepare", json=payload, headers=headers)).json()
        assert prep["status"] == "MISS"
        staged = await client.put(f"/v1/parts/data/{prep['transfer_id']}", content=data,
                                  headers={**headers, "X-Content-MD5-Hex": digest})
        assert staged.status_code == 200
        done = await wait(client, prep["execution_id"], headers)
        assert done["status"] == "SUCCEEDED"
        assert done["attempts"] == 2
        assert done["upstream_response"]["status_code"] == 200
        assert any(x[0].lower() == "etag" and x[1] == '"valid-md5"'
                   for x in done["upstream_response"]["headers"])
        assert len(calls) == 2
        assert all(content == data for _, content in calls)
        # Same MD5, different UploadId: data HIT, independent actual R2 PUT.
        payload["r2_request"]["url"] = (
            "https://abc.r2.cloudflarestorage.com/bucket/key?uploadId=other&partNumber=11"
        )
        hit = (await client.post("/v1/parts/prepare", json=payload, headers=headers)).json()
        assert hit["status"] == "HIT"
        done2 = await wait(client, hit["execution_id"], headers)
        assert done2["status"] == "SUCCEEDED"
        assert len(calls) == 3


@pytest.mark.asyncio
async def test_r2_4xx_not_retried(tmp_path):
    attempts = []
    def mock_r2(request):
        attempts.append(1)
        return httpx.Response(403, text="SignatureDoesNotMatch")
    app = create_app(CacheStore(tmp_path), token="test",
                     r2_transport=httpx.MockTransport(mock_r2), retry_delays=(0, 0, 0))
    headers = {"Authorization": "Bearer test"}
    data = b"small"
    digest = hashlib.md5(data).hexdigest()
    payload = {"request_id":"b","md5":digest,"size":len(data),
               "r2_request":{"method":"PUT",
                 "url":"https://abc.r2.cloudflarestorage.com/b/k?uploadId=x&partNumber=1"}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        prep=(await client.post("/v1/parts/prepare",json=payload,headers=headers)).json()
        await client.put(f"/v1/parts/data/{prep['transfer_id']}",content=data,
                         headers={**headers,"X-Content-MD5-Hex":digest})
        done=await wait(client,prep["execution_id"],headers)
        assert done["status"]=="FAILED"
        assert done["upstream_response"]["status_code"]==403
        assert len(attempts)==1

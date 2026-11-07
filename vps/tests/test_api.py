import hashlib

import httpx
import pytest

from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore


@pytest.mark.asyncio
async def test_prepare_miss_upload_hit_and_auth(tmp_path):
    app = create_app(CacheStore(tmp_path), token="testing")
    transport = httpx.ASGITransport(app=app)
    body = b"test-part"
    md5 = hashlib.md5(body).hexdigest()
    payload = {
        "schema_version": 1,
        "request_id": "r1",
        "md5": md5,
        "size": len(body),
        "r2_request": {
            "method": "PUT",
            "url": "https://a.r2.cloudflarestorage.com/bucket/key?uploadId=abc&partNumber=1",
            "headers": []
        }
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/v1/parts/prepare", json=payload)).status_code == 401
        h = {"Authorization": "Bearer testing"}
        miss = (await client.post("/v1/parts/prepare", json=payload, headers=h)).json()
        assert miss["status"] == "MISS"
        result = await client.put(f"/v1/parts/data/{miss['transfer_id']}",
                                  content=body, headers={**h, "X-Content-MD5-Hex": md5})
        assert result.status_code == 200
        assert result.json()["status"] == "STAGED"
        hit = (await client.post("/v1/parts/prepare", json=payload, headers=h)).json()
        assert hit["status"] == "HIT"
        # R2 worker is not yet implemented: no success can be fabricated.
        status = (await client.get(f"/v1/executions/{hit['execution_id']}", headers=h)).json()
        assert status["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_reject_non_r2_target(tmp_path):
    app = create_app(CacheStore(tmp_path), token="testing")
    payload = {"request_id":"r1","md5":"a"*32,"size":10,
               "r2_request":{"method":"PUT",
                             "url":"https://example.com/?uploadId=a&partNumber=1"}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        r = await client.post("/v1/parts/prepare", json=payload,
                              headers={"Authorization":"Bearer testing"})
        assert r.status_code == 400

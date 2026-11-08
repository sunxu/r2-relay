import asyncio
import base64
import hashlib

import httpx
import pytest

from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore


@pytest.mark.asyncio
async def test_r2_response_larger_than_old_64k_limit_is_preserved(tmp_path):
    response_body = b"E" * (128 * 1024)
    app = create_app(
        CacheStore(tmp_path), token="test",
        r2_transport=httpx.MockTransport(
            lambda _: httpx.Response(403, content=response_body, headers={"X-R2-Error": "full"})
        ),
        retry_delays=(0, 0, 0),
    )
    body = b"upload bytes"
    md5 = hashlib.md5(body).hexdigest()
    auth = {"Authorization": "Bearer test"}
    prepare = {"request_id": "full-response", "md5": md5, "size": len(body),
               "r2_request": {"method": "PUT",
                 "url": "https://abc.r2.cloudflarestorage.com/b/k?uploadId=abc&partNumber=1"}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        reply = (await client.post("/v1/parts/prepare", headers=auth, json=prepare)).json()
        await client.put("/v1/parts/data/" + reply["transfer_id"],
                         headers={**auth, "X-Content-MD5-Hex": md5}, content=body)
        for _ in range(100):
            result = (await client.get(
                "/v1/executions/" + reply["execution_id"], headers=auth
            )).json()
            if result["status"] in ("FAILED", "SUCCEEDED"):
                break
            await asyncio.sleep(0.01)
    assert result["status"] == "FAILED"
    assert result["upstream_response"]["status_code"] == 403
    assert base64.b64decode(result["upstream_response"]["body_base64"]) == response_body
    assert result["upstream_response"]["body_truncated"] is False

"""Wire-byte preservation for content-encoded R2 error responses."""
import asyncio
import base64
import gzip
import hashlib

import httpx
import pytest

from r2relay_local.addon import R2RelayAddon
from r2relay_local.relay import UpstreamResult
from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore
from test_addon import StubRelay, flow


@pytest.mark.asyncio
async def test_addon_does_not_double_encode_gzip_wire_body():
    original = b"<Error>SignatureDoesNotMatch</Error>"
    compressed = gzip.compress(original)
    addon = R2RelayAddon(StubRelay(UpstreamResult(
        403, [("Content-Encoding", "gzip"), ("Content-Type", "application/xml")],
        compressed,
    )))
    sample = flow(body=b"hello", length=5)
    addon.requestheaders(sample)
    await addon.request(sample)
    assert sample.response.status_code == 403
    assert sample.response.headers["Content-Encoding"] == "gzip"
    assert sample.response.raw_content == compressed
    assert sample.response.content == original
    assert int(sample.response.headers["Content-Length"]) == len(compressed)


@pytest.mark.asyncio
async def test_worker_records_original_gzip_wire_body(tmp_path):
    original = b"<Error>R2 escaped response</Error>"
    wire = gzip.compress(original)
    app = create_app(CacheStore(tmp_path), token="test",
                     r2_transport=httpx.MockTransport(lambda _: httpx.Response(
                         403, headers={"Content-Encoding": "gzip"}, content=wire
                     )), retry_delays=(0, 0, 0))
    body = b"hello"
    md5 = hashlib.md5(body).hexdigest()
    auth = {"Authorization": "Bearer test"}
    p = {"request_id": "gzip", "md5": md5, "size": len(body),
         "r2_request": {"method": "PUT",
                        "url": "https://acct.r2.cloudflarestorage.com/b/k?uploadId=x&partNumber=1"}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://vps"
    ) as client:
        prep = (await client.post("/v1/parts/prepare", json=p, headers=auth)).json()
        uploaded = await client.put("/v1/parts/data/" + prep["transfer_id"],
                                    content=body, headers={**auth, "X-Content-MD5-Hex": md5})
        assert uploaded.status_code == 200
        for _ in range(100):
            result = (await client.get("/v1/executions/" + prep["execution_id"],
                                       headers=auth)).json()
            if result["status"] in ("FAILED", "SUCCEEDED"):
                break
            await asyncio.sleep(0.01)
    assert result["status"] == "FAILED"
    response = result["upstream_response"]
    assert response["status_code"] == 403
    assert base64.b64decode(response["body_base64"]) == wire
    assert ("content-encoding", "gzip") in [
        (k.lower(), v) for k, v in response["headers"]
    ]

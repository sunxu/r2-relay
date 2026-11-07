import base64
import hashlib
from urllib.parse import urlsplit

import httpx
import pytest

from r2relay_local.relay import RelayClient, RelayUnavailable, is_upload_part

URL = "https://abc.r2.cloudflarestorage.com/b/k?uploadId=abc&partNumber=1"


def test_classification():
    assert is_upload_part("PUT", URL)
    assert not is_upload_part("POST", URL)
    assert not is_upload_part("PUT", "https://example.com/b?uploadId=x&partNumber=1")
    assert not is_upload_part("PUT", "https://abc.r2.cloudflarestorage.com/b/k?uploadId=x")
    assert not is_upload_part("PUT", "https://abc.r2.cloudflarestorage.com/b/k?uploadId=x&partNumber=0")


@pytest.mark.asyncio
@pytest.mark.parametrize("hit", [True, False])
async def test_prepare_hit_or_miss_returns_real_response(hit):
    requests = []
    body = b"sample bytes"
    digest = hashlib.md5(body).hexdigest()

    def handler(req):
        requests.append(req)
        assert urlsplit(str(req.url)).hostname == "vps.example"
        if req.url.path.endswith("/prepare"):
            assert req.headers["Authorization"] == "Bearer secret"
            assert req.content.find(digest.encode()) >= 0
            return httpx.Response(200, json={"status": "HIT" if hit else "MISS",
                                             "transfer_id": "tr1", "execution_id": "ex1"})
        if req.url.path.endswith("/data/tr1"):
            assert req.content == body
            assert req.headers["X-Content-MD5-Hex"] == digest
            return httpx.Response(200, json={"status": "STAGED"})
        return httpx.Response(200, json={"status": "SUCCEEDED",
                "upstream_response": {"status_code": 200,
                                      "headers": [["ETag", '"real-etag"']],
                                      "body_base64": base64.b64encode(b"").decode()}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        relay = RelayClient("https://vps.example:8443", "secret", client=http, poll_interval=0)
        result = await relay.upload(URL, [("Content-Length", str(len(body)))], body)
    assert result.status == 200
    assert result.headers == [("ETag", '"real-etag"')]
    assert len(requests) == (2 if hit else 3)


@pytest.mark.asyncio
async def test_vps_failure_causes_fallback_signal_not_fake_response():
    async def handler(request):
        raise httpx.ConnectError("offline")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        relay = RelayClient("https://vps.example:8443", "secret", client=http)
        with pytest.raises(RelayUnavailable):
            await relay.upload(URL, [], b"data")


@pytest.mark.asyncio
async def test_r2_502_from_vps_is_returned_without_direct_fallback():
    async def handler(request):
        if request.url.path.endswith("prepare"):
            return httpx.Response(200, json={"status":"HIT","execution_id":"ex"})
        return httpx.Response(200, json={"status":"FAILED","upstream_response":{
            "status_code":502,"headers":[["Content-Type","text/plain"]],
            "body_base64":base64.b64encode(b"R2 502").decode()}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        relay=RelayClient("https://vps.example:8443","secret",client=http,poll_interval=0)
        result=await relay.upload(URL,[],b"data")
    assert result.status==502
    assert result.body==b"R2 502"

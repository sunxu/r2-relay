import base64
import hashlib
import logging
from urllib.parse import urlsplit

import httpx
import pytest

from r2relay_local import relay as relay_module
from r2relay_local.relay import (
    DownloadPartTransportError,
    RelayClient,
    RelayUnavailable,
    is_upload_part,
    http2_enabled,
)

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


@pytest.mark.asyncio
@pytest.mark.parametrize("hit", [False, True])
async def test_relay_stage_timing_is_sanitized(caplog, hit):
    secret_url = URL + "&X-Amz-Signature=private-signature"
    body = b"timing payload"

    def handler(req):
        if req.url.path.endswith("/prepare"):
            return httpx.Response(200, json={
                "status": "HIT" if hit else "MISS",
                "execution_id": "private-execution-id",
                "transfer_id": "private-transfer-id",
            })
        if "/data/" in req.url.path:
            assert req.content == body
            return httpx.Response(200, json={"status": "STAGED"})
        return httpx.Response(200, json={
            "status": "FAILED",
            "upstream_response": {
                "status_code": 403,
                "headers": [],
                "body_base64": base64.b64encode(b"expired").decode(),
            },
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        relay = RelayClient("https://vps.example:8443", "private-token",
                            client=http, poll_interval=0)
        with caplog.at_level(logging.INFO, logger="r2relay.local.timing"):
            result = await relay.upload(secret_url, [], body)

    assert result.status == 403
    logs = [r.getMessage() for r in caplog.records
            if r.name == "r2relay.local.timing"]
    assert len(logs) == 1
    line = logs[0]
    assert f"cache={'HIT' if hit else 'MISS'}" in line
    assert f"size_bytes={len(body)}" in line
    assert "prepare_ms=" in line
    assert "execution_wait_ms=" in line
    assert "total_relay_ms=" in line
    assert "polls=1" in line
    assert "r2_status=403" in line
    assert "vps_http_version=HTTP/1.1" in line
    assert ("data_upload_ms=na" in line) == hit
    assert ("data_mbps=na" in line) == hit
    for sensitive in ("private-token", "private-signature",
                      "private-execution-id", "private-transfer-id",
                      secret_url, "timing payload"):
        assert sensitive not in line


@pytest.mark.parametrize(("value", "expected"), [
    (None, True), ("1", True), ("0", False),
])
def test_http2_env_selection(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("R2_RELAY_HTTP2", raising=False)
    else:
        monkeypatch.setenv("R2_RELAY_HTTP2", value)
    assert http2_enabled() is expected


@pytest.mark.parametrize("value", ["", "2", "true", "false", "yes", " 0"])
def test_http2_env_invalid_fails_closed(monkeypatch, value):
    monkeypatch.setenv("R2_RELAY_HTTP2", value)
    with pytest.raises(ValueError, match="R2_RELAY_HTTP2"):
        http2_enabled()


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "expected"), [("0", False), ("1", True)])
async def test_relay_client_sets_httpx_protocol_flag(monkeypatch, value, expected):
    actual_client = httpx.AsyncClient
    created = []

    def construct_client(**kwargs):
        created.append(kwargs)
        return actual_client(**kwargs)

    monkeypatch.setenv("R2_RELAY_HTTP2", value)
    monkeypatch.setattr(relay_module.httpx, "AsyncClient", construct_client)
    relay = RelayClient("https://vps.example:8443", "secret")
    try:
        assert len(created) == 1
        assert created[0]["http2"] is expected
        assert created[0]["trust_env"] is False
        assert created[0]["follow_redirects"] is False
    finally:
        await relay.close()



@pytest.mark.asyncio
async def test_part_stream_maps_mid_body_transport_failure_to_retryable_error():
    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("simulated connection reset")

        async def aclose(self):
            return None

    def handler(_request):
        return httpx.Response(
            200,
            headers={
                "Content-Length": "16",
                "X-Part-SHA256": "unused",
                "X-Part-Number": "1",
            },
            stream=InterruptedStream(),
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        relay = RelayClient("https://vps.example:8443", "secret", client=http)
        with pytest.raises(DownloadPartTransportError):
            async with relay.download_part_stream("task-id", 1) as response:
                async for _chunk in response.aiter_raw():
                    pass

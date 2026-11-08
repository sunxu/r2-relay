"""Local→VPS API→R2 Worker integration without external network."""
import asyncio
import hashlib

import httpx
import pytest

from r2relay_local.relay import RelayClient
from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore


@pytest.mark.asyncio
async def test_miss_hit_and_real_upstream_response_through_full_api_stack(tmp_path):
    r2_calls = []

    def r2_handler(request):
        r2_calls.append((str(request.url), request.content))
        if len(r2_calls) == 1:
            return httpx.Response(502, text="upstream transient error")
        return httpx.Response(200, headers={"ETag": '"from-r2"'})

    app = create_app(
        CacheStore(tmp_path),
        token="integration-token",
        r2_transport=httpx.MockTransport(r2_handler),
        retry_delays=(0, 0, 0),
    )
    body = b"10MB-style sample content"
    digest = hashlib.md5(body).hexdigest()
    url = "https://account.r2.cloudflarestorage.com/bucket/obj?uploadId=one&partNumber=10"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as transport_client:
        relay = RelayClient(
            "https://vps.example:8443", "integration-token",
            client=transport_client, poll_interval=0.005, execution_timeout=5,
        )
        result = await relay.upload(url, [("Content-Type", "application/octet-stream")], body)
        assert result.status == 200
        assert ("etag", '"from-r2"') in [
            (name.lower(), value) for name, value in result.headers
        ]
        assert len(r2_calls) == 2
        assert all(content == body for _, content in r2_calls)
        assert len(app.state.relay.store.entries) == 1
        assert digest in app.state.relay.store.entries

        # Second request is independent on R2, but uses same VPS cached bytes.
        other = url.replace("uploadId=one", "uploadId=two")
        next_result = await relay.upload(other, [], body)
        assert next_result.status == 200
        assert len(r2_calls) == 3
        assert r2_calls[-1][0].find("uploadId=two") >= 0

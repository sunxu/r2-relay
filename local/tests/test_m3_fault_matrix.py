"""M3: ambiguity, bounded resources and canonical URL regression tests."""
import asyncio
import base64
import hashlib
from pathlib import Path

import httpx
import pytest

from r2relay_local.relay import RelayClient, RelayOutcomeUnknown, RelayUnavailable
from r2relay_local.addon import R2RelayAddon
from r2relay_local.config import Settings
from test_addon import StubRelay, flow

R2_URL = (
    "https://acct.r2.cloudflarestorage.com/bucket/path%2Ffile.bin?"
    "uploadId=A%2FB%2BC&partNumber=17&X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=abc%2F20261107%2Fauto%2Fs3%2Faws4_request"
    "&X-Amz-SignedHeaders=host%3Bcontent-type&X-Amz-Signature=0123abcdef"
)


@pytest.mark.asyncio
async def test_prepare_response_lost_must_not_fallback_to_direct_r2():
    async def handler(request):
        assert request.url.path == "/v1/parts/prepare"
        raise httpx.ReadError("the request was sent, reply was lost")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        relay = RelayClient("https://vps.invalid:8443", "token", client=client)
        with pytest.raises(RelayOutcomeUnknown, match="outcome unknown"):
            await relay.upload(R2_URL, [], b"part")


@pytest.mark.asyncio
async def test_definitive_connect_failure_allows_direct_r2():
    async def handler(request):
        raise httpx.ConnectError("could not establish TCP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        relay = RelayClient("https://vps.invalid:8443", "token", client=client)
        with pytest.raises(RelayUnavailable, match="connect"):
            await relay.upload(R2_URL, [], b"part")


@pytest.mark.asyncio
async def test_request_url_and_signed_headers_are_forwarded_exactly():
    captured = []

    def handler(request):
        if request.url.path.endswith("/prepare"):
            import json
            data = json.loads(request.content)
            captured.append(data["r2_request"])
            return httpx.Response(200, json={"status": "HIT", "execution_id": "e1"})
        return httpx.Response(200, json={
            "status": "SUCCEEDED",
            "upstream_response": {
                "status_code": 200,
                "headers": [["ETag", '"123"']],
                "body_base64": base64.b64encode(b"").decode(),
            }
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        relay = RelayClient("https://vps.invalid:8443", "token", client=client)
        result = await relay.upload(R2_URL, [
            ("Host", "acct.r2.cloudflarestorage.com"),
            ("Content-Type", "application/octet-stream"),
            ("Connection", "keep-alive"),
        ], b"part")
    assert result.status == 200
    assert captured[0]["url"] == R2_URL
    assert ["Content-Type", "application/octet-stream"] in captured[0]["headers"]
    assert ["Host", "acct.r2.cloudflarestorage.com"] in captured[0]["headers"]
    assert not any(x[0].lower() == "connection" for x in captured[0]["headers"])


def test_parallel_memory_reservations_remain_within_budget():
    addon = R2RelayAddon(
        StubRelay(),
        settings=Settings(
            max_part_bytes=512 * 1024,
            global_budget_bytes=1024 * 1024,
            source=Path("test-config.toml"),
        ),
    )
    flows = [flow(body=b"", length=128 * 1024) for _ in range(24)]
    for candidate in flows:
        addon.requestheaders(candidate)
    assert addon.reserved_bytes == 1048576
    assert sum(bool(candidate.request.stream) for candidate in flows) == 16
    for candidate in flows:
        addon.error(candidate)
    assert addon.reserved_bytes == 0


@pytest.mark.asyncio
async def test_client_receives_full_128_kib_R2_error_body():
    body = b"E" * (128 * 1024)
    addon = R2RelayAddon(StubRelay(result=__import__(
        "r2relay_local.relay", fromlist=["UpstreamResult"]
    ).UpstreamResult(403, [("Content-Type", "application/xml")], body)))
    f = flow(body=b"part", length=4)
    addon.requestheaders(f)
    await addon.request(f)
    assert f.response.status_code == 403
    assert f.response.content == body
    assert addon.reserved_bytes == 0

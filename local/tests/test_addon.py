from pathlib import Path
from types import SimpleNamespace

import pytest
from mitmproxy import http

from r2relay_local.addon import R2RelayAddon
from r2relay_local.config import Settings
from r2relay_local.relay import RelayUnavailable, UpstreamResult

URL = "https://abc.r2.cloudflarestorage.com/b/k?uploadId=x&partNumber=1"


class StubRelay:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def upload(self, url, headers, body):
        self.calls.append((url, headers, body))
        if self.error is not None:
            raise self.error
        return self.result


def flow(url=URL, body=b"hello", method="PUT", length=None):
    headers = http.Headers()
    if length is not None:
        headers["content-length"] = str(length)
    return SimpleNamespace(
        request=SimpleNamespace(
            method=method, pretty_url=url, raw_content=body, headers=headers,
            stream=False,
        ),
        response=None,
    )


@pytest.mark.asyncio
async def test_r2_502_stays_r2_502_no_direct_fallback():
    relay = StubRelay(UpstreamResult(
        502, [("ETag", '"test"'), ("Content-Length", "100")], b"R2 failed"
    ))
    addon = R2RelayAddon(relay)
    f = flow(length=5)
    addon.requestheaders(f)
    await addon.request(f)
    assert len(relay.calls) == 1
    assert relay.calls[0][2] == b"hello"
    assert f.response.status_code == 502
    assert f.response.content == b"R2 failed"
    assert f.response.headers["Content-Length"] == str(len(b"R2 failed"))


@pytest.mark.asyncio
async def test_vps_unavailable_leaves_original_flow_for_direct_r2():
    relay = StubRelay(error=RelayUnavailable("offline"))
    addon = R2RelayAddon(relay)
    f = flow(length=5)
    addon.requestheaders(f)
    await addon.request(f)
    assert len(relay.calls) == 1
    assert f.response is None
    assert f.request.raw_content == b"hello"


@pytest.mark.asyncio
async def test_other_traffic_never_uses_relay():
    relay = StubRelay(UpstreamResult(200, [], b""))
    addon = R2RelayAddon(relay)
    f = flow(url="https://example.com/", body=b"noop", length=4)
    await addon.request(f)
    assert relay.calls == []
    assert f.response is None


def test_large_or_unknown_upload_is_preemptively_streamed():
    addon = R2RelayAddon(
        StubRelay(),
        settings=Settings(
            max_part_bytes=10,
            global_budget_bytes=20,
            source=Path("test-config.toml"),
        ),
    )
    for length in (11, None):
        f = flow(body=b"hello", length=length)
        addon.requestheaders(f)
        assert f.request.stream is True
    f = flow(body=b"hello", length=5)
    addon.requestheaders(f)
    assert f.request.stream is False



def test_global_buffer_budget_is_reserved_and_released():
    addon = R2RelayAddon(
        StubRelay(),
        settings=Settings(
            max_part_bytes=10,
            global_budget_bytes=10,
            source=Path("test-config.toml"),
        ),
    )
    first = flow(body=b"12345678", length=8)
    addon.requestheaders(first)
    assert first.request.stream is False
    assert addon.reserved_bytes == 8

    over_budget = flow(body=b"123", length=3)
    addon.requestheaders(over_budget)
    assert over_budget.request.stream is True
    assert addon.reserved_bytes == 8

    addon.error(first)
    assert addon.reserved_bytes == 0

    after_release = flow(body=b"123", length=3)
    addon.requestheaders(after_release)
    assert after_release.request.stream is False
    assert addon.reserved_bytes == 3

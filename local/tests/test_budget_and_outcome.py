import pytest

from mitmproxy import http

from r2relay_local.addon import R2RelayAddon
from r2relay_local.relay import RelayOutcomeUnknown, UpstreamResult

from test_addon import StubRelay, flow


def test_reservations_prevent_exceeding_global_memory_budget(monkeypatch):
    monkeypatch.setenv("R2_RELAY_BUFFER_BUDGET", "8")
    addon = R2RelayAddon(StubRelay(), max_part_bytes=20)
    first = flow(body=b"123456", length=6)
    second = flow(body=b"abcdef", length=6)
    addon.requestheaders(first)
    assert addon.reserved_bytes == 6
    addon.requestheaders(second)
    assert second.request.stream is True
    assert addon.reserved_bytes == 6
    addon.error(first)
    assert addon.reserved_bytes == 0


@pytest.mark.asyncio
async def test_unknown_outcome_never_bypasses_to_direct_r2():
    addon = R2RelayAddon(StubRelay(error=RelayOutcomeUnknown("R2 PUT may still be in flight")))
    sample = flow(body=b"abc", length=3)
    addon.requestheaders(sample)
    await addon.request(sample)
    assert sample.response.status_code == 502
    assert b"outcome unknown" in sample.response.content
    assert addon.reserved_bytes == 0


@pytest.mark.asyncio
async def test_reserved_memory_released_after_real_r2_result(monkeypatch):
    monkeypatch.setenv("R2_RELAY_BUFFER_BUDGET", "8")
    addon = R2RelayAddon(StubRelay(UpstreamResult(200, [("ETag", '"x"')], b"")))
    sample = flow(body=b"abc", length=3)
    addon.requestheaders(sample)
    assert addon.reserved_bytes == 3
    await addon.request(sample)
    assert addon.reserved_bytes == 0
    assert sample.response.status_code == 200

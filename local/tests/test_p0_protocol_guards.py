"""P0: ambiguous API results must never trigger a competing direct UploadPart."""
import httpx
import pytest

from r2relay_local.relay import (
    RelayClient, RelayOutcomeUnknown, RelayStorageUnavailable,
    is_upload_part,
)
from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore

R2_URL = "https://acct.r2.cloudflarestorage.com/b/k?uploadId=id&partNumber=1"


@pytest.mark.parametrize("bad_url", [
    "https://acct.r2.cloudflarestorage.com:garbage/b?uploadId=x&partNumber=1",
    "https://acct.r2.cloudflarestorage.com:99999/b?uploadId=x&partNumber=1",
    "https://[broken/b?uploadId=x&partNumber=1",
])
def test_malformed_ports_are_safely_not_intercepted(bad_url):
    assert is_upload_part("PUT", bad_url) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 500, 502, 503, 507])
async def test_prepare_api_error_never_authorizes_direct_put(status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status)
    )) as http:
        relay = RelayClient("https://vps.invalid:8443", "test", client=http)
        with pytest.raises(RelayOutcomeUnknown):
            await relay.upload(R2_URL, [], b"part")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    b"not json",
    b'{"status":"UNKNOWN","execution_id":"ex"}',
    b'{"status":"HIT"}',
    b'{"status":"MISS","execution_id":"ex"}',
])
async def test_malformed_prepare_is_unknown_not_fallback(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=body)
    )) as http:
        relay = RelayClient("https://vps.invalid:8443", "test", client=http)
        with pytest.raises(RelayOutcomeUnknown):
            await relay.upload(R2_URL, [], b"part")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 500, 502, 507])
async def test_result_query_error_after_hit_is_always_unknown(status):
    def handler(request):
        if request.url.path.endswith("prepare"):
            return httpx.Response(200, json={"status": "HIT", "execution_id": "e"})
        return httpx.Response(status)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        relay = RelayClient("https://vps.invalid:8443", "test", client=http)
        with pytest.raises(RelayOutcomeUnknown):
            await relay.upload(R2_URL, [], b"part")


@pytest.mark.asyncio
async def test_only_data_upload_507_allows_direct_fallback():
    def handler(request):
        if request.url.path.endswith("prepare"):
            return httpx.Response(
                200, json={"status": "MISS", "execution_id": "e", "transfer_id": "x"}
            )
        return httpx.Response(507)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        relay = RelayClient("https://vps.invalid:8443", "test", client=http)
        with pytest.raises(RelayStorageUnavailable):
            await relay.upload(R2_URL, [], b"part")


@pytest.mark.asyncio
async def test_vps_prepares_reject_over_limit_without_creating_task(tmp_path, monkeypatch):
    monkeypatch.setenv("R2_RELAY_MAX_PART_BYTES", "3")
    app = create_app(CacheStore(tmp_path), token="test")
    payload = {
        "request_id": "overlimit", "md5": "a" * 32, "size": 4,
        "r2_request": {"method": "PUT", "url": R2_URL},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.post(
            "/v1/parts/prepare", json=payload,
            headers={"Authorization": "Bearer test"},
        )
    assert response.status_code == 413
    assert app.state.relay.executions == {}
    assert app.state.relay.transfers == {}


@pytest.mark.asyncio
async def test_vps_rejects_invalid_target_port_as_400(tmp_path):
    app = create_app(CacheStore(tmp_path), token="test")
    payload = {
        "request_id": "badport", "md5": "b" * 32, "size": 4,
        "r2_request": {
            "method": "PUT",
            "url": "https://acct.r2.cloudflarestorage.com:invalid/b/k?uploadId=x&partNumber=1",
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.post(
            "/v1/parts/prepare", json=payload,
            headers={"Authorization": "Bearer test"},
        )
    assert response.status_code == 400
    assert app.state.relay.executions == {}

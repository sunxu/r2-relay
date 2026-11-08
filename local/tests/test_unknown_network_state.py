import httpx
import pytest

from r2relay_local.relay import RelayClient, RelayOutcomeUnknown


@pytest.mark.asyncio
async def test_vps_query_transport_lost_after_hit_is_unknown_not_direct():
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/prepare"):
            return httpx.Response(200, json={"status": "HIT", "execution_id": "ex"})
        raise httpx.ConnectError("VPS disconnected after execution began")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        relay = RelayClient("https://vps.example:8443", "test", client=http)
        with pytest.raises(RelayOutcomeUnknown, match="query failed"):
            await relay.upload(
                "https://abc.r2.cloudflarestorage.com/b/k?uploadId=x&partNumber=1",
                [], b"body"
            )
    assert requests == ["/v1/parts/prepare", "/v1/executions/ex"]


@pytest.mark.asyncio
async def test_vps_data_acknowledgement_lost_is_unknown():
    def handler(request):
        if request.url.path.endswith("/prepare"):
            return httpx.Response(200, json={"status": "MISS", "execution_id": "ex",
                                            "transfer_id": "tx"})
        raise httpx.ReadError("response lost after body upload")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        relay = RelayClient("https://vps.example:8443", "test", client=http)
        with pytest.raises(RelayOutcomeUnknown, match="acknowledgement"):
            await relay.upload(
                "https://abc.r2.cloudflarestorage.com/b/k?uploadId=x&partNumber=1",
                [], b"body"
            )

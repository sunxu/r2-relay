"""Real VPS ASGI server; test-only network transport to loopback Mock R2.

Injection is scoped to this fixture module. Production create_app() still uses
the real R2 URL and has no environment-controlled override for arbitrary SSRF.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx

from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore
from r2relay_vps.config import DownloadSettings


class _CloseStream(httpx.AsyncByteStream):
    def __init__(self, response: httpx.Response, client: httpx.AsyncClient):
        self.response = response
        self.client = client

    async def __aiter__(self):
        try:
            async for chunk in self.response.aiter_raw():
                yield chunk
        finally:
            await self.response.aclose()
            await self.client.aclose()

    async def aclose(self):
        await self.response.aclose()
        await self.client.aclose()


class MockR2NetworkTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        port = int(os.environ["R2_TEST_MOCK_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError("invalid test port")
        # Deliberately only ever sends requests to loopback.
        if request.method == "GET":
            client = httpx.AsyncClient(timeout=120, trust_env=False)
            headers = [
                ("X-Mock-Original-URL", str(request.url)),
                ("X-Mock-Original-Host", request.headers.get("host", "")),
            ]
            headers.extend(
                (key, value) for key, value in request.headers.multi_items()
                if key.lower() not in {
                    "host", "connection", "keep-alive", "proxy-connection",
                    "transfer-encoding", "te", "trailer", "upgrade",
                }
            )
            outbound = client.build_request(
                "GET", f"http://127.0.0.1:{port}/mock/download",
                headers=headers,
            )
            try:
                response = await client.send(outbound, stream=True)
            except BaseException:
                await client.aclose()
                raise
            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                stream=_CloseStream(response, client),
                request=request,
            )

        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            response = await client.put(
                f"http://127.0.0.1:{port}/mock/uploadpart",
                headers={
                    "X-Mock-Original-URL": str(request.url),
                    "X-Mock-Original-Host": request.headers.get("host", ""),
                    "Content-Type": request.headers.get("content-type", ""),
                },
                content=request.stream,
            )
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )


app = create_app(
    CacheStore(Path(os.environ["R2_TEST_CACHE_DIR"])),
    token=os.environ["R2_RELAY_TOKEN"],
    r2_transport=MockR2NetworkTransport(),
    download_transport=MockR2NetworkTransport(),
    download_settings=DownloadSettings(
        enabled=True,
        part_size_bytes=16 * 1024 * 1024,
        split_threshold_bytes=24 * 1024 * 1024,
        cache_ttl_seconds=600,
        max_cache_bytes=128 * 1024 * 1024,
        min_free_bytes=1,
        max_active_tasks=8,
        max_retained_tasks=32,
        max_waiters=64,
        wait_timeout_seconds=30,
    ),
    download_root=Path(os.environ["R2_TEST_DOWNLOAD_CACHE_DIR"]),
    retry_delays=(0, 0, 0),
)

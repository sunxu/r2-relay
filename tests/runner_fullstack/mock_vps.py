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


class MockR2NetworkTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        port = int(os.environ["R2_TEST_MOCK_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError("invalid test port")
        # Deliberately only ever sends requests to loopback.
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
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
    retry_delays=(0, 0, 0),
)

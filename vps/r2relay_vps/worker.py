"""R2 PUT execution with bounded retry and real upstream response capture."""
from __future__ import annotations

import asyncio
import base64
import random
from pathlib import Path
from typing import Any

import httpx

RETRYABLE = {429, 500, 502, 503, 504}
MAX_RESPONSE_BODY = 65536
HOP_HEADERS = {"connection", "proxy-connection", "keep-alive", "transfer-encoding",
               "te", "trailer", "upgrade", "proxy-authenticate", "proxy-authorization"}


async def file_chunks(path: Path):
    with path.open("rb") as stream:
        while True:
            chunk = await asyncio.to_thread(stream.read, 1024 * 1024)
            if not chunk:
                return
            yield chunk


class R2Worker:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None,
                 delays: tuple[float, ...] = (2, 4, 8)):
        self.transport = transport
        self.delays = delays

    async def execute(self, record: dict[str, Any], path: Path) -> None:
        spec = record["request"]["r2_request"]
        record["status"] = "RUNNING"
        headers = [(k, v) for k, v in spec["headers"] if k.lower() not in HOP_HEADERS]
        # Redirects must not be followed; each signed URL is the authority.
        async with httpx.AsyncClient(transport=self.transport, follow_redirects=False,
                                     timeout=httpx.Timeout(60.0, connect=15.0),
                                     trust_env=False) as client:
            for attempt in range(1, len(self.delays) + 2):
                record["attempts"] = attempt
                try:
                    if not path.is_file():
                        raise FileNotFoundError("cached part has expired or disappeared")
                    response = await client.request(
                        "PUT", spec["url"], headers=headers, content=file_chunks(path)
                    )
                    # Read and record only bounded diagnostic body bytes.
                    body = response.content[:MAX_RESPONSE_BODY]
                    record["upstream_response"] = {
                        "status_code": response.status_code,
                        "headers": [[k, v] for k, v in response.headers.multi_items()
                                    if k.lower() not in HOP_HEADERS],
                        "body_base64": base64.b64encode(body).decode("ascii"),
                        "body_truncated": len(response.content) > MAX_RESPONSE_BODY,
                    }
                    if 200 <= response.status_code < 300:
                        record["status"] = "SUCCEEDED"
                        return
                    if response.status_code not in RETRYABLE:
                        record["status"] = "FAILED"
                        return
                except (httpx.TransportError, OSError) as exc:
                    record["relay_error"] = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                    # Do not invent an R2 response for transport errors.
                    record.pop("upstream_response", None)
                    if isinstance(exc, FileNotFoundError):
                        record["status"] = "FAILED"
                        return
                if attempt > len(self.delays):
                    record["status"] = "FAILED"
                    return
                record["status"] = "RETRY_WAIT"
                await asyncio.sleep(self.delays[attempt - 1] + random.uniform(0, 0.25))
                record["status"] = "RUNNING"

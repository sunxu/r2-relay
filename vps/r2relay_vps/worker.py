"""R2 PUT execution with bounded retry and real upstream response capture."""
from __future__ import annotations

import asyncio
import base64
import logging
import random
from pathlib import Path
from typing import Any

import httpx

RETRYABLE = {429, 500, 502, 503, 504}
MAX_RESPONSE_BODY = 8 * 1024 * 1024
LOG = logging.getLogger("r2relay.worker")
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
        # Presigned S3 UploadPart expects a fixed body length, not chunked encoding.
        if not any(k.lower() == "content-length" for k, _ in headers):
            headers.append(("Content-Length", str(path.stat().st_size)))
        # Redirects must not be followed; each signed URL is the authority.
        async with httpx.AsyncClient(transport=self.transport, follow_redirects=False,
                                     timeout=httpx.Timeout(60.0, connect=15.0),
                                     trust_env=False) as client:
            for attempt in range(1, len(self.delays) + 2):
                record["attempts"] = attempt
                LOG.info("R2 PUT start attempt=%s md5=%s url=%s",
                         attempt, record["request"]["md5"], spec["url"])
                try:
                    if not path.is_file():
                        raise FileNotFoundError("cached part has expired or disappeared")
                    async with client.stream(
                        "PUT", spec["url"], headers=headers, content=file_chunks(path)
                    ) as response:
                        body = bytearray()
                        async for chunk in response.aiter_raw():
                            if len(body) + len(chunk) > MAX_RESPONSE_BODY:
                                record["relay_error"] = {
                                    "type": "ResponseTooLarge",
                                    "message": "R2 response exceeds 8 MiB",
                                }
                                record.pop("upstream_response", None)
                                record["status"] = "FAILED"
                                return
                            body.extend(chunk)
                        record["upstream_response"] = {
                            "status_code": response.status_code,
                            "headers": [[k, v] for k, v in response.headers.multi_items()
                                        if k.lower() not in HOP_HEADERS],
                            "body_base64": base64.b64encode(body).decode("ascii"),
                            "body_truncated": False,
                        }
                        LOG.info("R2 PUT result attempt=%s status=%s etag=%s url=%s",
                                 attempt, response.status_code,
                                 response.headers.get("etag", ""), spec["url"])
                        if 200 <= response.status_code < 300:
                            record["status"] = "SUCCEEDED"
                            return
                        if response.status_code not in RETRYABLE:
                            record["status"] = "FAILED"
                            return
                except (httpx.TransportError, OSError) as exc:
                    LOG.warning("R2 PUT exception attempt=%s url=%s error=%s",
                                attempt, spec["url"], exc)
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

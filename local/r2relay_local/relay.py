"""Local-memory MD5 negotiation client. Does not manufacture R2 success."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import base64
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx

HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-connection", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authenticate", "proxy-authorization",
})
EXECUTION_TERMINAL = frozenset({"SUCCEEDED", "FAILED"})
LOG = logging.getLogger("r2relay.local.timing")


def http2_enabled() -> bool:
    """Read the opt-in protocol override; preserve HTTP/2 as the default."""
    value = os.environ.get("R2_RELAY_HTTP2", "1")
    if value not in ("0", "1"):
        raise ValueError("R2_RELAY_HTTP2 must be 0 (HTTP/1.1) or 1 (HTTP/2)")
    return value == "1"


def is_r2_get(method: str, url: str) -> bool:
    """Match all GET requests to the same HTTPS R2 endpoint boundary as uploads."""
    if method.upper() != "GET":
        return False
    try:
        parts = urlsplit(url)
        allowed_port = parts.port in (None, 443)
    except ValueError:
        return False
    hostname = parts.hostname or ""
    return (
        parts.scheme == "https"
        and allowed_port
        and hostname.endswith(".r2.cloudflarestorage.com")
        and hostname != ".r2.cloudflarestorage.com"
    )


def is_upload_part(method: str, url: str) -> bool:
    if method.upper() != "PUT":
        return False
    try:
        parts = urlsplit(url)
        allowed_port = parts.port in (None, 443)
    except ValueError:
        return False
    if parts.scheme != "https" or not allowed_port:
        return False
    hostname = parts.hostname or ""
    if not hostname.endswith(".r2.cloudflarestorage.com"):
        return False
    query = parse_qs(parts.query, keep_blank_values=False)
    try:
        return bool(query["uploadId"][0]) and int(query["partNumber"][0]) >= 1
    except (KeyError, IndexError, ValueError):
        return False


@dataclass(frozen=True)
class UpstreamResult:
    status: int
    headers: list[tuple[str, str]]
    body: bytes


class RelayUnavailable(Exception):
    """A VPS transport/protocol failure; caller may bypass to original R2."""


class RelayStorageUnavailable(RelayUnavailable):
    """A definitive VPS cache write refusal, before any R2 worker starts."""


class RelayOutcomeUnknown(Exception):
    """VPS may already be uploading to R2; do not start a competing direct PUT."""


class RelayClient:
    def __init__(
        self, endpoint: str, token: str, *,
        client: httpx.AsyncClient | None = None,
        request_timeout: float = 15.0,
        execution_timeout: float = 180.0,
        poll_interval: float = 0.2,
    ):
        if not endpoint.startswith("https://") and client is None:
            raise ValueError("VPS relay must use HTTPS")
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            http2=http2_enabled(), timeout=request_timeout, trust_env=False,
            verify=os.environ.get("R2_RELAY_VPS_CA_FILE") or True,
            follow_redirects=False,
        )
        self.execution_timeout = execution_timeout
        self.poll_interval = poll_interval
        self.download_http_versions: list[str] = []

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            result = await self.client.request(
                method, self.endpoint + path,
                headers={"Authorization": f"Bearer {self.token}", **kwargs.pop("headers", {})},
                **kwargs,
            )
            if result.status_code == 507:
                if method == "PUT" and path.startswith("/v1/parts/data/"):
                    # Only the data endpoint promises no R2 worker started.
                    raise RelayStorageUnavailable("VPS rejected cache write before R2")
                raise RelayOutcomeUnknown("unexpected VPS HTTP 507 outside cache staging")
            if result.status_code >= 500:
                # A reverse proxy can emit 5xx after the backend has accepted
                # Prepare and started a HIT PUT. Only failed TCP connect is
                # provably safe for a direct fallback.
                raise RelayOutcomeUnknown(f"VPS HTTP {result.status_code} is ambiguous")
            if result.status_code >= 400:
                # Authentication/configuration and invalid protocol are not
                # evidence of network unavailability; never bypass to R2.
                raise RelayOutcomeUnknown(f"VPS protocol HTTP {result.status_code}")
            return result
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Connection was not established: safe to bypass the VPS.
            raise RelayUnavailable(f"VPS connect: {type(exc).__name__}") from exc
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            # After a connection is established, a lost acknowledgement is
            # ambiguous. The VPS might have started the R2 UploadPart already.
            raise RelayOutcomeUnknown(f"VPS request outcome unknown: {type(exc).__name__}") from exc

    async def _download_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = await self.client.request(
                method,
                self.endpoint + path,
                headers={"Authorization": f"Bearer {self.token}", **kwargs.pop("headers", {})},
                **kwargs,
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise RelayOutcomeUnknown(
                f"VPS 下载请求结果未知：{type(exc).__name__}"
            ) from exc
        self.download_http_versions.append(response.http_version)
        LOG.info(
            "download_relay_http_version=%s method=%s",
            response.http_version, method,
        )
        if response.status_code >= 400:
            raise RelayOutcomeUnknown(
                f"VPS 下载 API 返回 HTTP {response.status_code}"
            )
        return response

    async def download_create(
        self, request_id: str, url: str, headers: list[tuple[str, str]]
    ) -> dict:
        response = await self._download_request(
            "POST",
            "/v1/downloads",
            json={
                "schema_version": 1,
                "request_id": request_id,
                "url": url,
                "headers": [[key, value] for key, value in headers if key.lower() not in HOP_HEADERS],
            },
            timeout=15.0,
        )
        if response.status_code != 202:
            raise RelayOutcomeUnknown("VPS 未接受下载任务")
        try:
            result = response.json()
            if not result.get("task_id"):
                raise ValueError("missing task_id")
            return result
        except (ValueError, TypeError) as exc:
            raise RelayOutcomeUnknown("VPS 下载任务响应格式无效") from exc

    async def download_manifest(self, task_id: str) -> dict:
        response = await self._download_request(
            "GET", f"/v1/downloads/{task_id}",
            timeout=15.0,
        )
        try:
            result = response.json()
            if result.get("task_id") != task_id:
                raise ValueError("task_id mismatch")
            return result
        except (ValueError, TypeError) as exc:
            raise RelayOutcomeUnknown("VPS 下载 manifest 格式无效") from exc

    @asynccontextmanager
    async def download_part_stream(self, task_id: str, part_number: int):
        try:
            async with self.client.stream(
                "GET",
                self.endpoint + f"/v1/downloads/{task_id}/parts/{part_number}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=httpx.Timeout(90.0, connect=15.0, read=90.0, write=30.0),
            ) as response:
                yield response
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise RelayOutcomeUnknown(
                f"VPS Part 取回结果未知：{type(exc).__name__}"
            ) from exc

    async def download_body(self, task_id: str, *, max_bytes: int) -> bytes:
        body = bytearray()
        try:
            async with self.client.stream(
                "GET",
                self.endpoint + f"/v1/downloads/{task_id}/body",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=httpx.Timeout(90.0, connect=15.0, read=90.0, write=30.0),
            ) as response:
                if response.status_code != 200:
                    raise RelayOutcomeUnknown(
                        f"VPS 小响应 API 返回 HTTP {response.status_code}"
                    )
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > max_bytes:
                        raise RelayOutcomeUnknown("VPS 小响应超过本地内存上限")
                    body.extend(chunk)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise RelayOutcomeUnknown(
                f"VPS 小响应取回结果未知：{type(exc).__name__}"
            ) from exc
        return bytes(body)

    async def download_delete(self, task_id: str) -> None:
        try:
            response = await self.client.request(
                "DELETE",
                self.endpoint + f"/v1/downloads/{task_id}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=15.0,
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise RelayOutcomeUnknown(
                f"VPS 下载清理确认未知：{type(exc).__name__}"
            ) from exc
        if response.status_code not in (200, 404):
            raise RelayOutcomeUnknown(
                f"VPS 下载清理 API 返回 HTTP {response.status_code}"
            )

    async def upload(
        self, url: str, headers: list[tuple[str, str]], body: bytes
    ) -> UpstreamResult:
        started_at = time.perf_counter()
        content_md5 = hashlib.md5(body, usedforsecurity=False).hexdigest()
        prepare = {
            "schema_version": 1,
            "request_id": __import__("secrets").token_urlsafe(12),
            "md5": content_md5,
            "size": len(body),
            "r2_request": {
                "method": "PUT",
                "url": url,
                "headers": [[k, v] for k, v in headers if k.lower() not in HOP_HEADERS],
            },
        }
        reply = await self._request("POST", "/v1/parts/prepare", json=prepare)
        prepared_at = time.perf_counter()
        vps_http_version = reply.http_version
        data_upload_ms: float | None = None
        data_mbps: float | None = None
        try:
            decision = reply.json()
            status = decision["status"]
            execution_id = decision["execution_id"]
        except (ValueError, TypeError, KeyError) as exc:
            raise RelayOutcomeUnknown("VPS invalid prepare response") from exc
        if status == "MISS":
            try:
                transfer = decision["transfer_id"]
            except KeyError as exc:
                raise RelayOutcomeUnknown("VPS missing transfer_id") from exc
            try:
                data_started_at = time.perf_counter()
                await self._request(
                    "PUT", f"/v1/parts/data/{transfer}",
                    headers={"X-Content-MD5-Hex": content_md5,
                             "Content-Type": "application/octet-stream"},
                    content=body,
                )
                data_upload_ms = (time.perf_counter() - data_started_at) * 1000
                if data_upload_ms > 0:
                    data_mbps = len(body) * 8 / (data_upload_ms * 1000)
            except RelayStorageUnavailable:
                # 507 is explicitly returned before any R2 worker can start.
                raise
            except (RelayUnavailable, RelayOutcomeUnknown) as exc:
                # The VPS might have staged the full part and begun R2 PUT.
                raise RelayOutcomeUnknown("VPS data transfer acknowledgement was lost") from exc
        elif status != "HIT":
            raise RelayOutcomeUnknown(f"VPS invalid cache status {status!r}")

        execution_started_at = time.perf_counter()
        poll_count = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.execution_timeout
        while loop.time() < deadline:
            try:
                result = await self._request("GET", f"/v1/executions/{execution_id}")
                poll_count += 1
            except RelayUnavailable as exc:
                raise RelayOutcomeUnknown("VPS task exists but result query failed") from exc
            try:
                data = result.json()
                current = data["status"]
            except (ValueError, TypeError, KeyError) as exc:
                raise RelayOutcomeUnknown("VPS malformed execution result") from exc
            if current in EXECUTION_TERMINAL:
                upstream = data.get("upstream_response")
                if upstream:
                    if upstream.get("body_truncated"):
                        raise RelayOutcomeUnknown("VPS truncated R2 response")
                    try:
                        upstream_result = UpstreamResult(
                            status=int(upstream["status_code"]),
                            headers=[(str(k), str(v)) for k, v in upstream["headers"]
                                     if str(k).lower() not in HOP_HEADERS],
                            body=base64.b64decode(upstream["body_base64"], validate=True),
                        )
                    except (KeyError, ValueError, TypeError) as exc:
                        raise RelayOutcomeUnknown("VPS malformed R2 result") from exc
                    finished_at = time.perf_counter()
                    LOG.info(
                        "relay_timing cache=%s size_bytes=%d prepare_ms=%.1f "
                        "data_upload_ms=%s data_mbps=%s execution_wait_ms=%.1f "
                        "total_relay_ms=%.1f polls=%d r2_status=%d vps_http_version=%s",
                        status, len(body), (prepared_at - started_at) * 1000,
                        f"{data_upload_ms:.1f}" if data_upload_ms is not None else "na",
                        f"{data_mbps:.2f}" if data_mbps is not None else "na",
                        (finished_at - execution_started_at) * 1000,
                        (finished_at - started_at) * 1000, poll_count,
                        upstream_result.status, vps_http_version,
                    )
                    return upstream_result
                # No R2 status existed. Do not claim the error came from R2.
                reason = data.get("relay_error", {})
                raise RelayOutcomeUnknown(f"VPS execution ended without R2 response: {reason}")
            if current not in ("QUEUED", "RUNNING", "RETRY_WAIT"):
                raise RelayOutcomeUnknown(f"VPS invalid execution status {current!r}")
            await asyncio.sleep(self.poll_interval)
        raise RelayOutcomeUnknown("VPS execution timeout; R2 result is unknown")

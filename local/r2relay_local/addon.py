"""Mitmproxy addon. In default mode, request() runs after full body buffering."""
from __future__ import annotations

import asyncio
import logging
import os

from mitmproxy import http

from .config import Settings, load_settings
from .relay import RelayClient, RelayUnavailable, RelayOutcomeUnknown, is_upload_part, is_r2_get
from .download import DownloadCoordinator
from .disk_response import DiskResponseServer

LOG = logging.getLogger("r2relay.local")


class R2RelayAddon:
    def __init__(
        self, relay: RelayClient, *, settings: Settings | None = None,
        download_coordinator: DownloadCoordinator | None = None,
        disk_response_server: DiskResponseServer | None = None,
    ):
        self.relay = relay
        self.settings = settings or load_settings()
        self.downloads = download_coordinator
        if self.downloads is None and self.settings.download_enabled:
            self.downloads = DownloadCoordinator(relay, self.settings)
        self.disk_responses = disk_response_server or DiskResponseServer()
        self.max_part_bytes = self.settings.max_part_bytes
        self.global_budget_bytes = self.settings.global_budget_bytes
        LOG.info(
            "upload buffer config loaded source=%s max_part_mib=%d global_buffer_mib=%d",
            self.settings.source,
            self.settings.max_part_mib,
            self.settings.global_buffer_mib,
        )
        self.reserved_bytes = 0
        self.reservations: dict[int, int] = {}

    def _release(self, flow: http.HTTPFlow) -> None:
        held = self.reservations.pop(id(flow), 0)
        self.reserved_bytes = max(0, self.reserved_bytes - held)

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        """Avoid buffering huge/unknown-size request bodies at the MITM layer."""
        req = flow.request
        if not is_upload_part(req.method, req.pretty_url):
            return
        raw_length = req.headers.get("content-length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if (length < 0 or length > self.max_part_bytes
                or self.reserved_bytes + length > self.global_budget_bytes):
            req.stream = True
            LOG.info("bypass buffered relay: unknown/oversized body or memory budget exhausted")
            return
        self.reserved_bytes += length
        self.reservations[id(flow)] = length

    async def request(self, flow: http.HTTPFlow) -> None:
        req = flow.request
        if is_r2_get(req.method, req.pretty_url) and self.downloads is not None:
            original_headers = [
                (k, v) for k, v in req.headers.items(multi=True)
                if k.lower() not in {
                    "connection", "keep-alive", "proxy-connection",
                    "transfer-encoding", "te", "trailer", "upgrade",
                }
            ]
            result = None
            try:
                result = await self.downloads.download(req.pretty_url, original_headers)
                token = await self.disk_responses.register(result)
                flow.metadata["r2relay_download_token"] = token
                flow.metadata["r2relay_download"] = True
                req.scheme = "http"
                req.host = "127.0.0.1"
                req.port = int(self.disk_responses.port)
                req.path = f"/download/{token}"
                req.headers.clear()
                req.headers["Host"] = f"127.0.0.1:{self.disk_responses.port}"
                req.headers["Connection"] = "close"
            except asyncio.CancelledError:
                if result is not None and result.cleanup is not None:
                    await result.cleanup()
                raise
            except Exception as exc:
                if result is not None and result.cleanup is not None:
                    asyncio.get_running_loop().create_task(result.cleanup())
                LOG.error("R2 GET download relay failed: %s", type(exc).__name__)
                flow.response = http.Response.make(
                    status_code=502,
                    content=b"R2 Relay: download failed; no direct R2 fallback was attempted",
                    headers={"Content-Type": "text/plain"},
                )
            return
        if not is_upload_part(req.method, req.pretty_url):
            return
        if getattr(req, "stream", False):
            return
        body = req.raw_content
        reserved = self.reservations.get(id(flow), 0)
        if body is None or len(body) > self.max_part_bytes or len(body) > reserved:
            self._release(flow)
            # mitmproxy continues forwarding the original request to R2.
            return
        try:
            result = await self.relay.upload(
                req.pretty_url,
                [(k, v) for k, v in req.headers.items(multi=True)],
                body,
            )
        except RelayOutcomeUnknown as exc:
            LOG.error("R2 outcome unknown; refusing a competing direct PUT: %s", exc)
            self._release(flow)
            flow.response = http.Response.make(
                status_code=502,
                content=b"R2 Relay: upload outcome unknown; retry through original client",
                headers={"Content-Type": "text/plain"},
            )
            return
        except RelayUnavailable as exc:
            LOG.warning("VPS unavailable; forwarding original request directly to R2: %s", exc)
            self._release(flow)
            return
        self._release(flow)
        # Set response only for confirmed real upstream HTTP result.
        # mitmproxy owns framing, so drop hop-by-hop / stale length headers.
        response_headers = [(k, v) for k, v in result.headers
                            if k.lower() not in {"content-length", "transfer-encoding",
                                                 "connection", "keep-alive", "proxy-connection",
                                                 "te", "trailer", "upgrade"}]
        # .make(content=...) applies Content-Encoding from headers. Preserve
        # the exact wire bytes already received from R2 instead of encoding twice.
        encodings = [(k, v) for k, v in response_headers
                     if k.lower() == "content-encoding"]
        plain_headers = [(k, v) for k, v in response_headers
                         if k.lower() != "content-encoding"]
        flow.response = http.Response.make(
            status_code=result.status,
            content=result.body,
            headers=[(k.encode("latin-1"), v.encode("latin-1"))
                     for k, v in plain_headers],
        )
        for name, value in encodings:
            flow.response.headers.add(name, value)
        flow.response.headers["Content-Length"] = str(len(result.body))

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if getattr(flow, "metadata", {}).get("r2relay_download"):
            flow.response.stream = True

    def error(self, flow: http.HTTPFlow) -> None:
        self._release(flow)
        token = getattr(flow, "metadata", {}).get("r2relay_download_token")
        if token:
            try:
                asyncio.get_running_loop().create_task(self.disk_responses.discard(token))
            except RuntimeError:
                pass

    async def done(self) -> None:
        await self.disk_responses.close()
        if self.downloads is not None:
            await self.downloads.close()
        await self.relay.close()


def build_addon() -> R2RelayAddon:
    endpoint = os.environ.get("R2_RELAY_VPS_URL", "https://127.0.0.1:8443")
    token = os.environ.get("R2_RELAY_TOKEN", "")
    if not token:
        raise RuntimeError("R2_RELAY_TOKEN missing")
    return R2RelayAddon(RelayClient(endpoint, token))


addons = [build_addon()] if os.environ.get("R2_RELAY_ENABLE_ADDON") == "1" else []

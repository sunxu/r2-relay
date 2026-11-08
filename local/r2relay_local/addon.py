"""Mitmproxy addon. In default mode, request() runs after full body buffering."""
from __future__ import annotations

import logging
import os

from mitmproxy import http

from .relay import RelayClient, RelayUnavailable, RelayOutcomeUnknown, is_upload_part

LOG = logging.getLogger("r2relay.local")


class R2RelayAddon:
    def __init__(self, relay: RelayClient, *, max_part_bytes: int = 32 * 1024 * 1024):
        self.relay = relay
        self.max_part_bytes = max_part_bytes
        self.global_budget_bytes = int(os.environ.get("R2_RELAY_BUFFER_BUDGET", str(512 * 1024 * 1024)))
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

    def error(self, flow: http.HTTPFlow) -> None:
        self._release(flow)

    async def done(self) -> None:
        await self.relay.close()


def build_addon() -> R2RelayAddon:
    endpoint = os.environ.get("R2_RELAY_VPS_URL", "https://127.0.0.1:8443")
    token = os.environ.get("R2_RELAY_TOKEN", "")
    if not token:
        raise RuntimeError("R2_RELAY_TOKEN missing")
    return R2RelayAddon(RelayClient(endpoint, token))


addons = [build_addon()] if os.environ.get("R2_RELAY_ENABLE_ADDON") == "1" else []

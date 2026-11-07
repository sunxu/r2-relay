"""Mitmproxy addon. In default mode, request() runs after full body buffering."""
from __future__ import annotations

import logging
import os

from mitmproxy import http

from .relay import RelayClient, RelayUnavailable, is_upload_part

LOG = logging.getLogger("r2relay.local")


class R2RelayAddon:
    def __init__(self, relay: RelayClient, *, max_part_bytes: int = 32 * 1024 * 1024):
        self.relay = relay
        self.max_part_bytes = max_part_bytes

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
        if length < 0 or length > self.max_part_bytes:
            req.stream = True
            LOG.info("bypass buffered relay for oversized/unknown-length UploadPart")

    async def request(self, flow: http.HTTPFlow) -> None:
        req = flow.request
        if not is_upload_part(req.method, req.pretty_url):
            return
        body = req.raw_content
        if body is None or len(body) > self.max_part_bytes:
            # mitmproxy continues forwarding the original request to R2.
            return
        try:
            result = await self.relay.upload(
                req.pretty_url,
                [(k, v) for k, v in req.headers.items(multi=True)],
                body,
            )
        except RelayUnavailable as exc:
            LOG.warning("VPS unavailable; forwarding original request directly to R2: %s", exc)
            return
        # Set response only for confirmed real upstream HTTP result.
        # mitmproxy owns framing, so drop hop-by-hop / stale length headers.
        response_headers = [(k, v) for k, v in result.headers
                            if k.lower() not in {"content-length", "transfer-encoding",
                                                 "connection", "keep-alive", "proxy-connection",
                                                 "te", "trailer", "upgrade"}]
        flow.response = http.Response.make(
            status_code=result.status,
            content=result.body,
            headers=[(k.encode("utf-8", "surrogateescape"),
                      v.encode("utf-8", "surrogateescape"))
                     for k, v in response_headers],
        )


def build_addon() -> R2RelayAddon:
    endpoint = os.environ.get("R2_RELAY_VPS_URL", "https://127.0.0.1:8443")
    token = os.environ.get("R2_RELAY_TOKEN", "")
    if not token:
        raise RuntimeError("R2_RELAY_TOKEN missing")
    return R2RelayAddon(RelayClient(endpoint, token))


addons = [build_addon()] if os.environ.get("R2_RELAY_ENABLE_ADDON") == "1" else []

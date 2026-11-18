"""PoC addon: redirect one intercepted HTTPS request to a local disk file server.

The local server is the upstream HTTP body source. mitmproxy streams that upstream
response to the original HTTPS client instead of constructing a large synthetic
http.Response body in memory.
"""
from __future__ import annotations

import os

from mitmproxy import http


class DiskBackedResponsePoC:
    def __init__(self) -> None:
        self.origin_port = int(os.environ["R2_POC_ORIGIN_PORT"])

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.request.pretty_host != "r2-poc.example":
            return
        flow.request.scheme = "http"
        flow.request.host = "127.0.0.1"
        flow.request.port = self.origin_port
        flow.request.path = "/file"
        flow.request.headers["Host"] = f"127.0.0.1:{self.origin_port}"

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if flow.request.host == "127.0.0.1" and flow.request.port == self.origin_port:
            flow.response.stream = True


addons = [DiskBackedResponsePoC()]

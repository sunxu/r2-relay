"""概念验证 addon：将一个被拦截的 HTTPS 请求转发到本机磁盘文件服务。

本机文件服务作为上游 HTTP 响应体来源。mitmproxy 将该上游响应流式转发给
原始 HTTPS 客户端，避免在内存中构造大型 http.Response 响应体。
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

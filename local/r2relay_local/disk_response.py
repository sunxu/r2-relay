"""仅监听 127.0.0.1 的磁盘/内存 HTTP 响应源，供 mitmproxy 流式转发。"""
from __future__ import annotations

import asyncio
import secrets
from http import HTTPStatus
from urllib.parse import urlsplit

from .download import DownloadResult

HOP_HEADERS = {
    "connection", "keep-alive", "proxy-connection", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authenticate", "proxy-authorization",
}


def _response_headers(result: DownloadResult) -> list[tuple[str, str]]:
    nominated: set[str] = set()
    for key, value in result.headers:
        if key.lower() == "connection":
            nominated.update(x.strip().lower() for x in value.split(",") if x.strip())
    blocked = HOP_HEADERS | nominated
    headers = [(k, v) for k, v in result.headers if k.lower() not in blocked]
    status = result.status_code
    bodyless = status in (204, 304) or 100 <= status < 200
    if status == 204 or 100 <= status < 200:
        headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
    elif not bodyless:
        headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
        headers.append(("Content-Length", str(result.size)))
    headers.append(("Connection", "close"))
    return headers


class DiskResponseServer:
    """把已完整验证的结果作为 loopback 上游响应流交给 mitmproxy。"""

    def __init__(self):
        self.server: asyncio.AbstractServer | None = None
        self.port: int | None = None
        self.routes: dict[str, DownloadResult] = {}
        self.writers: set[asyncio.StreamWriter] = set()
        self.handlers: set[asyncio.Task] = set()
        self.closed = False

    async def start(self) -> None:
        if self.closed:
            raise RuntimeError("本地磁盘响应服务已关闭")
        if self.server is not None:
            return
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, limit=64 * 1024
        )
        self.port = int(self.server.sockets[0].getsockname()[1])

    async def register(self, result: DownloadResult) -> str:
        await self.start()
        token = secrets.token_urlsafe(24)
        self.routes[token] = result
        return token

    async def discard(self, token: str) -> None:
        result = self.routes.pop(token, None)
        if result is not None and result.cleanup is not None:
            await result.cleanup()

    async def _write_body(self, writer: asyncio.StreamWriter, result: DownloadResult) -> None:
        status = result.status_code
        if status in (204, 304) or 100 <= status < 200:
            return
        sent = 0
        if result.body is not None:
            view = memoryview(result.body)
            while sent < len(view):
                chunk = view[sent:sent + 1024 * 1024]
                writer.write(chunk)
                await writer.drain()
                sent += len(chunk)
        elif result.path is not None:
            with result.path.open("rb", buffering=0) as stream:
                while True:
                    chunk = await asyncio.to_thread(stream.read, 1024 * 1024)
                    if not chunk:
                        break
                    writer.write(chunk)
                    await writer.drain()
                    sent += len(chunk)
        if sent != result.size:
            raise OSError("本地响应文件长度与已验证长度不一致")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        current = asyncio.current_task()
        if current is not None:
            self.handlers.add(current)
        self.writers.add(writer)
        token: str | None = None
        result: DownloadResult | None = None
        try:
            request_line = await reader.readline()
            fields = request_line.decode("ascii", errors="replace").strip().split()
            if len(fields) != 3 or fields[0] != "GET":
                raise ValueError("仅接受本地 GET 请求")
            await reader.readuntil(b"\r\n\r\n")
            path = urlsplit(fields[1]).path
            if not path.startswith("/download/"):
                raise ValueError("本地下载路由无效")
            token = path.removeprefix("/download/")
            result = self.routes.get(token)
            if result is None:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return

            try:
                reason = HTTPStatus(result.status_code).phrase
            except ValueError:
                reason = "Unknown"
            head = [f"HTTP/1.1 {result.status_code} {reason}\r\n"]
            for key, value in _response_headers(result):
                head.append(f"{key}: {value}\r\n")
            head.append("\r\n")
            writer.write("".join(head).encode("latin-1"))
            await writer.drain()
            await self._write_body(writer, result)
        except (asyncio.IncompleteReadError, BrokenPipeError, ConnectionResetError, OSError, ValueError):
            # 原始客户端可能中途断开；无论如何都要释放本地响应资源。
            pass
        finally:
            if token is not None and result is not None:
                await self.discard(token)
            self.writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            if current is not None:
                self.handlers.discard(current)

    async def close(self) -> None:
        self.closed = True
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for writer in tuple(self.writers):
            writer.close()
        handlers = [task for task in self.handlers if task is not asyncio.current_task()]
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        for token in tuple(self.routes):
            await self.discard(token)

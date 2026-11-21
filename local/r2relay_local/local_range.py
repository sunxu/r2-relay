"""Optional direct-to-R2 GET backend with bounded, ordered 2 MiB Range reads.

The initial GET is never replayed: it either remains the single source or
supplies the first verified chunk. Only immutable, strongly identified object
responses are eligible for sub-Range requests.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import fcntl
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import parse_qs, urlsplit

import httpx

from .config import Settings
from .download import (
    DownloadRelayError, DownloadResult, WeightedBudget,
    _pwrite_all, _run_disk_operation,
)
from .relay import http2_enabled, is_r2_get

_MIB = 1024 * 1024
_IO_BLOCK = 64 * 1024
_RANGE = re.compile(r"bytes=(\d+)-(\d*)\Z", re.I)
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)\Z", re.I)
_SIGNED = re.compile(r"SignedHeaders=([^,\s]+)", re.I)
_CONDITIONAL = frozenset({
    "if-match", "if-none-match", "if-modified-since",
    "if-unmodified-since", "if-range",
})


class LocalRangeError(DownloadRelayError):
    """R2 direct response could not be returned or safely assembled."""


class _Progress:
    def __init__(self):
        self.ready: set[int] = set()
        self.changed = asyncio.Event()
        self.consumed = 0


def _length(response: httpx.Response) -> int | None:
    values = response.headers.get_list("content-length")
    if not values:
        return None
    if len(values) != 1:
        raise LocalRangeError("ambiguous upstream Content-Length")
    try:
        value = int(values[0])
    except ValueError as exc:
        raise LocalRangeError("invalid upstream Content-Length") from exc
    if value < 0:
        raise LocalRangeError("negative upstream Content-Length")
    return value


def _strong_etag(response: httpx.Response) -> str | None:
    values = response.headers.get_list("etag")
    if len(values) != 1:
        return None
    value = values[0]
    if not value.startswith('"') or not value.endswith('"') or value.startswith('W/') or "," in value:
        return None
    return value


def _signed_headers(url: str, headers: list[tuple[str, str]]) -> set[str] | None:
    query = {k.lower(): v for k, v in parse_qs(urlsplit(url).query).items()}
    if "x-amz-signature" in query:
        signed = query.get("x-amz-signedheaders")
        if not signed or len(signed) != 1:
            return None
        return set(signed[0].lower().split(";"))
    auth = [v for k, v in headers if k.lower() == "authorization"]
    if auth:
        if len(auth) != 1 or not auth[0].startswith("AWS4-HMAC-SHA256 "):
            return None
        match = _SIGNED.search(auth[0])
        if match is None:
            return None
        return set(match.group(1).lower().split(";"))
    return set()


def _plan(
    response: httpx.Response, url: str, headers: list[tuple[str, str]],
    length: int | None, chunk_size: int, minimum: int,
) -> tuple[int, int, str, int] | None:
    """Return (absolute_first_byte, response_length, etag, object_size) or None."""
    if response.status_code not in (200, 206) or length is None or length <= chunk_size:
        return None
    if length < minimum or response.headers.get("content-encoding", "identity").lower() != "identity":
        return None
    etag = _strong_etag(response)
    if etag is None:
        return None
    names = [k.lower() for k, _ in headers]
    if any(k in _CONDITIONAL for k in names) or names.count("range") > 1:
        return None
    signed = _signed_headers(url, headers)
    if signed is None or signed.intersection({"range", "if-match", "if-range"}):
        return None
    original_range = next((v for k, v in headers if k.lower() == "range"), None)
    if original_range is not None:
        match = _RANGE.fullmatch(original_range.strip())
        if match is None or response.status_code != 206:
            return None
        requested_start = int(match.group(1))
        requested_end = int(match.group(2)) if match.group(2) else None
        match = _CONTENT_RANGE.fullmatch(response.headers.get("content-range", ""))
        if match is None:
            return None
        first, last, total = map(int, match.groups())
        if (first != requested_start or last < first or total <= last
                or last - first + 1 != length
                or (requested_end is not None and last > requested_end)):
            return None
        return first, length, etag, total
    if response.status_code != 200 or response.headers.get("accept-ranges", "").lower() != "bytes":
        return None
    if response.headers.get("content-range") is not None:
        return None
    return 0, length, etag, length


class LocalRangeDownloader:
    """Direct R2 downloader; does not start or communicate with the VPS."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None):
        if (settings.download_local_chunk_bytes != 2 * _MIB
                or settings.download_local_min_bytes != 2 * _MIB):
            raise ValueError("local Range size and minimum must be 2 MiB")
        if not 1 <= settings.download_local_max_connections_per_get <= 8:
            raise ValueError("local max_connections_per_get out of range")
        if not (settings.download_local_max_connections_per_get
                <= settings.download_local_global_max_connections <= 128):
            raise ValueError("local global_max_connections out of range")
        self.settings = settings
        self.chunk_size = settings.download_local_chunk_bytes
        self.connections = asyncio.Semaphore(settings.download_local_global_max_connections)
        self.disk_budget = WeightedBudget(settings.download_max_temp_bytes)
        self._budget_lock = asyncio.Lock()
        self.root = settings.download_temp_dir / "local"
        self.client = client or httpx.AsyncClient(
            http2=http2_enabled(), trust_env=False, follow_redirects=False, verify=True,
            headers={},  # do not inject HTTPX Accept-Encoding into the original GET
            limits=httpx.Limits(
                max_connections=settings.download_local_global_max_connections,
                max_keepalive_connections=min(20, settings.download_local_global_max_connections),
            ),
            timeout=httpx.Timeout(90.0, connect=15.0, read=90.0, write=30.0),
        )
        self._owns_client = client is None
        self._cleanups: set = set()
        self.active: set[asyncio.Task] = set()
        self._closed = False
        self._lock_file = None
        if settings.download_enabled:
            if settings.download_temp_dir.is_symlink() or self.root.is_symlink():
                raise RuntimeError("Local download directory must not be a symlink")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            self._lock_file = (self.root / ".process.lock").open("a+b")
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self._lock_file.close()
                self._lock_file = None
                raise RuntimeError("Local download directory in use") from exc
            for child in self.root.iterdir():
                if child.name == ".process.lock":
                    continue
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child)

    def _request(self, url: str, headers: list[tuple[str, str]]) -> httpx.Request:
        """Keep signed/original end-to-end headers; remove HTTPX implicit defaults.

        Injected Accept-Encoding can make an otherwise identical GET return a
        different representation. Host remains HTTPX-owned for the R2 URL.
        """
        request = self.client.build_request("GET", url, headers=headers)
        original_names = {name.lower() for name, _ in headers}
        for name in ("accept", "accept-encoding", "user-agent", "connection"):
            if name not in original_names:
                request.headers.pop(name, None)
        return request

    @asynccontextmanager
    async def _stream(self, url: str, headers: list[tuple[str, str]]):
        response = await self.client.send(
            self._request(url, headers), stream=True, follow_redirects=False,
        )
        try:
            yield response
        finally:
            await response.aclose()

    async def _read_chunk(
        self, response: httpx.Response, fd: int, offset: int, expected: int,
    ) -> None:
        received = 0
        async for data in response.aiter_raw(chunk_size=_IO_BLOCK):
            if not data:
                continue
            if received + len(data) > expected:
                raise LocalRangeError("R2 chunk exceeds declared byte range")
            await _run_disk_operation(_pwrite_all, fd, data, offset + received)
            received += len(data)
            if received == expected:
                break
        if received != expected:
            raise LocalRangeError("R2 chunk was truncated")

    async def _child(
        self, url: str, headers: list[tuple[str, str]],
        first: int, length: int, etag: str, total: int,
        last_modified: str | None,
        index: int, fd: int, per_get: asyncio.Semaphore, progress: _Progress,
    ) -> None:
        offset = index * self.chunk_size
        part_len = min(self.chunk_size, length - offset)
        start = first + offset
        end = start + part_len - 1
        child_headers = [(k, v) for k, v in headers if k.lower() != "range"]
        child_headers.extend((("Range", f"bytes={start}-{end}"), ("If-Match", etag)))
        for attempt in range(3):
            try:
                async with per_get:
                    async with self.connections:
                        async with self._stream(url, child_headers) as response:
                            got = response.headers.get("content-range", "")
                            parsed = _CONTENT_RANGE.fullmatch(got)
                            if (response.status_code != 206 or parsed is None
                                    or int(parsed.group(1)) != start
                                    or int(parsed.group(2)) != end
                                    or int(parsed.group(3)) != total
                                    or _length(response) != part_len
                                    or _strong_etag(response) != etag
                                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                                    or response.headers.get("last-modified") != last_modified):
                                raise LocalRangeError("R2 sub-Range response/version mismatch")
                            await self._read_chunk(response, fd, offset, part_len)
                progress.ready.add(index)
                progress.changed.set()
                return
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == 2:
                    raise LocalRangeError(f"R2 Range transport failed: {type(exc).__name__}") from None
                await asyncio.sleep((0.15, 0.5)[attempt])

    async def _produce(
        self, initial: httpx.Response, release_initial,
        url: str, headers: list[tuple[str, str]],
        first: int, length: int, etag: str, total: int,
        last_modified: str | None,
        fd: int, progress: _Progress, per_get: asyncio.Semaphore,
    ) -> None:
        count = (length + self.chunk_size - 1) // self.chunk_size
        children: dict[int, asyncio.Task] = {}
        first_task: asyncio.Task | None = None

        async def initial_chunk():
            try:
                await self._read_chunk(initial, fd, 0, self.chunk_size)
                progress.ready.add(0)
                progress.changed.set()
            finally:
                await release_initial()

        try:
            first_task = asyncio.create_task(initial_chunk())
            children[0] = first_task
            next_index = 1
            while children or next_index < count:
                # At most eight scheduled chunks and eight unconsumed chunks.
                while (next_index < count
                       and len(children) < self.settings.download_local_max_connections_per_get
                       and next_index < progress.consumed + self.settings.download_local_max_connections_per_get):
                    index = next_index
                    children[index] = asyncio.create_task(self._child(
                        url, headers, first, length, etag, total, last_modified,
                        index, fd, per_get, progress,
                    ))
                    next_index += 1
                if not children:
                    progress.changed.clear()
                    if next_index >= progress.consumed + self.settings.download_local_max_connections_per_get:
                        await progress.changed.wait()
                    continue
                done, _ = await asyncio.wait(
                    children.values(), return_when=asyncio.FIRST_COMPLETED,
                )
                for index, task in tuple(children.items()):
                    if task in done:
                        await task
                        del children[index]
        finally:
            for task in children.values():
                if not task.done():
                    task.cancel()
            if children:
                await asyncio.gather(*children.values(), return_exceptions=True)
            await release_initial()
            progress.changed.set()

    async def download_stream(self, url: str, headers: list[tuple[str, str]]) -> DownloadResult:
        if not self.settings.download_enabled or self._closed:
            raise LocalRangeError("Local R2 download not enabled")
        if not is_r2_get("GET", url):
            raise LocalRangeError("R2 host is not allowed")
        current = asyncio.current_task()
        if current is not None:
            self.active.add(current)
        per_get = asyncio.Semaphore(self.settings.download_local_max_connections_per_get)
        response: httpx.Response | None = None
        init_held = False
        disk_reserved = 0
        fd = -1
        path: Path | None = None
        registered = False
        producer: asyncio.Task | None = None

        async def release_initial() -> None:
            nonlocal init_held
            if not init_held:
                return
            init_held = False
            try:
                if response is not None:
                    await response.aclose()
            finally:
                self.connections.release()
                per_get.release()

        try:
            await per_get.acquire()
            await self.connections.acquire()
            init_held = True
            response = await self.client.send(
                self._request(url, headers), stream=True, follow_redirects=False,
            )
            original_headers = list(response.headers.multi_items())
            expected = _length(response)
            status = response.status_code
            bodyless = status in (204, 304) or 100 <= status < 200
            if bodyless:
                expected = 0
            plan = _plan(
                response, url, headers, expected, self.chunk_size,
                self.settings.download_local_min_bytes,
            )
            can_split = (
                plan is not None
                and expected is not None
                and expected <= self.disk_budget.limit
                and shutil.disk_usage(self.settings.download_temp_dir).free >= expected
            )
            if can_split:
                # Never hold the replacement headers waiting on another slow
                # consumer's disk reservation: retain the original single GET.
                async with self._budget_lock:
                    if self.disk_budget.used + expected > self.disk_budget.limit:
                        can_split = False
                    else:
                        await self.disk_budget.acquire(expected)
                        disk_reserved = expected
            if can_split:
                fd, raw_path = tempfile.mkstemp(prefix="r2-range-", suffix=".bin", dir=self.root)
                path = Path(raw_path)
                os.fchmod(fd, 0o600)
                progress = _Progress()
                producer = asyncio.create_task(self._produce(
                    response, release_initial, url, headers,
                    plan[0], expected, plan[2], plan[3],
                    response.headers.get("last-modified"),
                    fd, progress, per_get,
                ))
                self.active.add(producer)
                producer.add_done_callback(self.active.discard)
            cleaned = False
            cleanup_lock = asyncio.Lock()

            async def cleanup() -> None:
                nonlocal cleaned, fd
                async with cleanup_lock:
                    if cleaned:
                        return
                    cleaned = True
                    self._cleanups.discard(cleanup)
                    if producer is not None:
                        if not producer.done():
                            producer.cancel()
                        await asyncio.gather(producer, return_exceptions=True)
                    await release_initial()
                    if fd >= 0:
                        os.close(fd)
                        fd = -1
                    if path is not None:
                        path.unlink(missing_ok=True)
                    if disk_reserved:
                        await self.disk_budget.release(disk_reserved)

            self._cleanups.add(cleanup)

            async def body_stream():
                try:
                    if bodyless:
                        return
                    if producer is None:
                        count = 0
                        async for chunk in response.aiter_raw(chunk_size=_IO_BLOCK):
                            count += len(chunk)
                            if expected is not None and count > expected:
                                raise LocalRangeError("R2 original stream longer than Content-Length")
                            if chunk:
                                yield chunk
                        if expected is not None and count != expected:
                            raise LocalRangeError("R2 original stream truncated")
                    else:
                        count = (expected + self.chunk_size - 1) // self.chunk_size
                        for index in range(count):
                            while index not in progress.ready:
                                if producer.done():
                                    await producer
                                    raise LocalRangeError("missing READY Range chunk")
                                progress.changed.clear()
                                if index not in progress.ready:
                                    if producer.done():
                                        await producer
                                        raise LocalRangeError("missing READY Range chunk")
                                    await progress.changed.wait()
                            if producer.done():
                                await producer
                            remaining = min(self.chunk_size, expected - index * self.chunk_size)
                            offset = index * self.chunk_size
                            while remaining:
                                amount = min(_IO_BLOCK, remaining)
                                chunk = await _run_disk_operation(os.pread, fd, amount, offset)
                                if len(chunk) != amount:
                                    raise LocalRangeError("READY chunk truncated on disk")
                                yield chunk
                                offset += amount
                                remaining -= amount
                            progress.consumed = index + 1
                            progress.changed.set()
                        await producer
                finally:
                    await cleanup()

            registered = True
            return DownloadResult(
                status_code=status, headers=original_headers,
                size=expected if expected is not None else -1,
                stream=body_stream(), cleanup=cleanup,
            )
        finally:
            if not registered:
                if producer is not None:
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
                await release_initial()
                if fd >= 0:
                    os.close(fd)
                if path is not None:
                    path.unlink(missing_ok=True)
                if disk_reserved:
                    await self.disk_budget.release(disk_reserved)
            if current is not None:
                self.active.discard(current)

    async def close(self) -> None:
        self._closed = True
        for task in tuple(self.active):
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in self.active if task is not asyncio.current_task()),
            return_exceptions=True,
        )
        for cleanup in tuple(self._cleanups):
            await cleanup()
        if self._owns_client:
            await self.client.aclose()
        if self._lock_file is not None:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

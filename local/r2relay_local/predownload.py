"""Optional same-name pre-placed R2 object response, before local or remote backend.

A local file is only eligible when a separate GET has supplied genuine R2
response headers and its object size matches. Neither the preview GET nor
this module consumes the R2 response body. Size is not a content hash.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit

import httpx

from .config import Settings
from .download import DownloadResult, _run_disk_operation
from .relay import http2_enabled, is_r2_get

LOG = logging.getLogger("r2relay.local.predownload")
_BLOCK = 1024 * 1024
_RANGE = re.compile(r"bytes=(\d+)-(\d*)\Z", re.I)
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)\Z", re.I)


def _filename(url: str) -> str | None:
    """Only a plain basename is a candidate, never a path or directory."""
    try:
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1], errors="strict")
    except (UnicodeError, ValueError):
        return None
    if name in ("", ".", "..") or "/" in name or "\\" in name or "\x00" in name:
        return None
    return name


def _length(response: httpx.Response) -> int | None:
    values = response.headers.get_list("content-length")
    if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
        return None
    return int(values[0])


def _match(
    response: httpx.Response, request_headers: list[tuple[str, str]], file_size: int,
) -> tuple[int, int] | None:
    """Return local (offset, body_length), or None for a normal backend miss."""
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        return None
    length = _length(response)
    if length is None:
        return None
    ranges = [v for k, v in request_headers if k.lower() == "range"]
    if not ranges:
        if response.status_code == 200 and length == file_size:
            return 0, length
        return None
    if len(ranges) != 1 or response.status_code != 206:
        return None
    wanted = _RANGE.fullmatch(ranges[0].strip())
    actual = _CONTENT_RANGE.fullmatch(response.headers.get("content-range", ""))
    if wanted is None or actual is None:
        return None
    first, last, total = map(int, actual.groups())
    start = int(wanted.group(1))
    requested_end = int(wanted.group(2)) if wanted.group(2) else None
    effective_end = min(requested_end, total - 1) if requested_end is not None else total - 1
    if (total != file_size or first != start or last != effective_end
            or first > last or last >= total or last - first + 1 != length):
        return None
    return first, length


def _local_result(
    fd: int, start: int, length: int, response_status: int,
    response_headers: list[tuple[str, str]],
) -> DownloadResult:
    lock = asyncio.Lock()
    closed = False

    async def cleanup() -> None:
        nonlocal closed
        async with lock:
            if not closed:
                closed = True
                os.close(fd)

    async def body_stream():
        offset = start
        remaining = length
        try:
            while remaining:
                chunk = await _run_disk_operation(
                    os.pread, fd, min(remaining, _BLOCK), offset,
                )
                if not chunk:
                    raise OSError("pre-placed file truncated during download")
                yield chunk
                offset += len(chunk)
                remaining -= len(chunk)
        finally:
            await cleanup()

    return DownloadResult(
        status_code=response_status,
        headers=response_headers,
        size=length,
        stream=body_stream(),
        cleanup=cleanup,
    )


class PredownloadDownloader:
    """Wrap an existing downloader, returning only verified-size local hits."""

    def __init__(
        self, backend, settings: Settings, *, client: httpx.AsyncClient | None = None,
    ):
        self.backend = backend
        root: Path = settings.download_predownload_dir
        if root.is_symlink():
            raise ValueError("predownload directory cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._directory_fd = os.open(
            root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            headers=[], http2=http2_enabled(), verify=True, trust_env=False,
            follow_redirects=False, timeout=httpx.Timeout(5.0, connect=3.0),
        )

    def _candidate(self, url: str) -> int | None:
        filename = _filename(url)
        if filename is None:
            return None
        try:
            fd = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=self._directory_fd,
            )
        except OSError:
            return None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        return fd

    async def _try_local(
        self, url: str, headers: list[tuple[str, str]],
    ) -> DownloadResult | None:
        if not is_r2_get("GET", url):
            return None
        # Unsupported/multiple ranges are not probed, preserving backend behavior.
        ranges = [v for k, v in headers if k.lower() == "range"]
        if len(ranges) > 1 or (ranges and _RANGE.fullmatch(ranges[0].strip()) is None):
            return None
        fd = self._candidate(url)
        if fd is None:
            return None
        try:
            # httpx.Request does not add Accept-Encoding or other client defaults:
            # the original signed request semantics remain unchanged.
            request = httpx.Request("GET", url, headers=headers)
            response = await self.client.send(request, stream=True, follow_redirects=False)
            try:
                matched = _match(response, headers, os.fstat(fd).st_size)
                if matched is None:
                    return None
                status = response.status_code
                response_headers = list(response.headers.multi_items())
            finally:
                await response.aclose()  # Headers only; never consume the body.
            result = _local_result(fd, *matched, status, response_headers)
            fd = -1  # Result owns the pinned file descriptor until cleanup.
            LOG.info("predownload_hit status=%d bytes=%d", status, matched[1])
            return result
        except asyncio.CancelledError:
            raise
        except (OSError, httpx.HTTPError, ValueError) as exc:
            # A broken probe never changes the original selected backend behavior.
            LOG.warning("predownload_probe_unavailable reason=%s", type(exc).__name__)
            return None
        finally:
            if fd >= 0:
                os.close(fd)

    async def download_stream(
        self, url: str, headers: list[tuple[str, str]],
    ) -> DownloadResult:
        matched = await self._try_local(url, headers)
        if matched is not None:
            return matched
        return await self.backend.download_stream(url, headers)

    async def close(self) -> None:
        try:
            if self._owns_client:
                await self.client.aclose()
        finally:
            os.close(self._directory_fd)
            await self.backend.close()

"""独立的 R2 GET 下载任务、磁盘分片与原子发布实现。"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit

import httpx

from .config import DownloadSettings

HOP_HEADERS = {
    "connection", "keep-alive", "proxy-connection", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authenticate", "proxy-authorization",
}


class DownloadError(Exception):
    status_code = 502


class DownloadDisabled(DownloadError):
    status_code = 503


class DownloadCapacity(DownloadError):
    status_code = 503


class DownloadNotFound(DownloadError):
    status_code = 404


class DownloadConflict(DownloadError):
    status_code = 409


class DownloadWaitTimeout(DownloadError):
    status_code = 504


class DownloadFailed(DownloadError):
    status_code = 502


def validate_r2_get_url(url: str, headers: list[tuple[str, str]] | None = None) -> None:
    try:
        parts = urlsplit(url)
        valid_port = parts.port in (None, 443)
    except ValueError as exc:
        raise ValueError("R2 GET URL 端口无效") from exc
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or not valid_port
        or not host.endswith(".r2.cloudflarestorage.com")
        or host == ".r2.cloudflarestorage.com"
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("仅允许 HTTPS 443 的 Cloudflare R2 S3 endpoint")
    signed_hosts = [value for key, value in (headers or []) if key.lower() == "host"]
    if any(value.lower() != host.lower() for value in signed_hosts):
        raise ValueError("R2 GET 请求中的 Host 必须与 URL 主机名一致")


def filter_hop_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    nominated = set()
    for key, value in headers:
        if key.lower() == "connection":
            nominated.update(part.strip().lower() for part in value.split(",") if part.strip())
    blocked = HOP_HEADERS | nominated
    return [(key, value) for key, value in headers if key.lower() not in blocked]


@asynccontextmanager
async def _stream_get(
    client: httpx.AsyncClient,
    url: str,
    headers: list[tuple[str, str]],
):
    """Send a manually constructed request without AsyncClient default headers."""
    request = httpx.Request("GET", url, headers=headers)
    response = await client.send(
        request, stream=True, follow_redirects=False,
    )
    try:
        yield response
    finally:
        await response.aclose()


async def _raw_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
    if response.is_stream_consumed:
        if response.content:
            yield response.content
        return
    async for chunk in response.aiter_raw():
        if chunk:
            yield chunk


@dataclass
class DownloadTask:
    task_id: str
    request_id: str
    fingerprint: str
    url: str
    request_headers: list[tuple[str, str]]
    directory: Path
    created_at: float
    last_access: float
    status: str = "QUEUED"
    mode: str | None = None
    status_code: int | None = None
    response_headers: list[tuple[str, str]] = field(default_factory=list)
    expected_size: int | None = None
    total_size: int = 0
    stored_bytes: int = 0
    parts: list[dict] = field(default_factory=list)
    error: dict | None = None
    expires_at: float | None = None
    writer_active: bool = False
    active_readers: int = 0
    waiters: int = 0
    delete_requested: bool = False
    worker: asyncio.Task | None = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


class PartWriter:
    """将连续字节流写成完整 Part；只有 fsync + 原子改名后才发布 READY。"""

    def __init__(self, manager: "DownloadManager", task: DownloadTask):
        self.manager = manager
        self.task = task
        self.number = 1
        self.size = 0
        self.digest = hashlib.sha256()
        self.file = None
        self.tmp_path: Path | None = None
        self.part_record: dict | None = None

    async def _open(self) -> None:
        if self.file is None:
            self.tmp_path = self.task.directory / f"{self.number}.part.tmp"
            self.file = self.tmp_path.open("xb")
            self.part_record = {
                "part_number": self.number,
                "size": 0,
                "sha256": None,
                "status": "WRITING",
            }
            self.task.parts.append(self.part_record)

    async def write(self, data: bytes, *, reserve: bool = True) -> None:
        offset = 0
        while offset < len(data):
            await self._open()
            take = min(
                len(data) - offset,
                self.manager.settings.part_size_bytes - self.size,
            )
            piece = data[offset:offset + take]
            if reserve:
                await self.manager.reserve(self.task, len(piece))
            else:
                self.manager.check_disk_reserve(len(piece))
            await asyncio.to_thread(self.file.write, piece)
            self.digest.update(piece)
            self.size += len(piece)
            self.task.total_size += len(piece)
            offset += len(piece)
            if self.size == self.manager.settings.part_size_bytes:
                await self._publish()

    async def _publish(self) -> None:
        if self.file is None or self.tmp_path is None or self.size == 0:
            return
        await asyncio.to_thread(self.file.flush)
        await asyncio.to_thread(os.fsync, self.file.fileno())
        self.file.close()
        final_path = self.task.directory / f"{self.number}.part"
        os.replace(self.tmp_path, final_path)
        directory_fd = os.open(self.task.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self.part_record["size"] = self.size
        self.part_record["sha256"] = self.digest.hexdigest()
        self.part_record["status"] = "READY"
        self.number += 1
        self.size = 0
        self.digest = hashlib.sha256()
        self.file = None
        self.tmp_path = None
        self.part_record = None
        async with self.task.condition:
            self.task.condition.notify_all()

    async def finish(self) -> None:
        if self.file is not None:
            await self._publish()

    async def abort(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
        if self.tmp_path is not None:
            self.tmp_path.unlink(missing_ok=True)
            self.tmp_path = None
        if self.part_record is not None:
            self.part_record["status"] = "FAILED"
            self.part_record = None


class DownloadManager:
    def __init__(
        self,
        root: Path,
        settings: DownloadSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock=time.time,
    ):
        self.root = Path(root)
        self.settings = settings
        self.transport = transport
        self.clock = clock
        self.tasks: dict[str, DownloadTask] = {}
        self.request_index: dict[str, str] = {}
        self.used_bytes = 0
        self.waiter_count = 0
        self.lock = asyncio.Lock()
        self._process_lock = None
        if settings.enabled:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink():
                raise RuntimeError("VPS 下载缓存目录不得是符号链接")
            os.chmod(self.root, 0o700)
            lock_path = self.root / ".process.lock"
            self._process_lock = lock_path.open("a+b")
            try:
                fcntl.flock(self._process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self._process_lock.close()
                self._process_lock = None
                raise RuntimeError(
                    "VPS 下载缓存目录已被另一个 worker 使用；下载管理器要求单进程独占"
                ) from exc
            os.chmod(lock_path, 0o600)
            # Task manifests are in-memory only. Remove orphaned task directories
            # from a previous process before accepting work and resetting budgets.
            for child in self.root.iterdir():
                if child.name == ".process.lock":
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                elif child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)

    @staticmethod
    def _fingerprint(url: str, headers: list[tuple[str, str]]) -> str:
        raw = json.dumps([url, headers], ensure_ascii=False, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    async def create(self, request_id: str, url: str, headers: list[tuple[str, str]]) -> DownloadTask:
        if not self.settings.enabled:
            raise DownloadDisabled("VPS 下载中继尚未启用；需先配置磁盘预算和任务上限")
        if not request_id or len(request_id) > 128:
            raise ValueError("request_id 必须为 1～128 个字符")
        if len(url) > 16384:
            raise ValueError("R2 GET URL 长度超过 16 KiB")
        if len(headers) > 128 or sum(len(k) + len(v) for k, v in headers) > 65536:
            raise ValueError("R2 GET 请求头数量或总长度超过限制")
        headers = filter_hop_headers(headers)
        validate_r2_get_url(url, headers)
        fingerprint = self._fingerprint(url, headers)
        async with self.lock:
            existing_id = self.request_index.get(request_id)
            if existing_id:
                existing = self.tasks.get(existing_id)
                if existing is not None:
                    if existing.fingerprint != fingerprint:
                        raise DownloadConflict("request_id 已用于不同的 URL 或请求头")
                    return existing
            active = sum(1 for item in self.tasks.values()
                         if item.status in {"QUEUED", "RECEIVING"})
            if active >= self.settings.max_active_tasks:
                raise DownloadCapacity("VPS 下载活动任务数已达上限")
            if len(self.tasks) >= self.settings.max_retained_tasks:
                raise DownloadCapacity("VPS 下载任务记录数已达上限，请等待 TTL 清理")
            task_id = secrets.token_urlsafe(24)
            directory = self.root / task_id
            directory.mkdir(mode=0o700)
            now = self.clock()
            task = DownloadTask(
                task_id=task_id, request_id=request_id, fingerprint=fingerprint,
                url=url, request_headers=headers, directory=directory,
                created_at=now, last_access=now,
            )
            self.tasks[task_id] = task
            self.request_index[request_id] = task_id
            task.worker = asyncio.create_task(self._run(task))
            return task

    def check_disk_reserve(self, incoming: int) -> None:
        free = shutil.disk_usage(self.root).free
        if free - incoming < self.settings.min_free_bytes:
            raise OSError("下载缓存磁盘空间低于配置的剩余空间保护值")

    async def reserve(self, task: DownloadTask, amount: int) -> None:
        async with self.lock:
            if self.used_bytes + amount > self.settings.max_cache_bytes:
                raise OSError("下载缓存达到配置的磁盘预算上限")
            self.check_disk_reserve(amount)
            self.used_bytes += amount
            task.stored_bytes += amount

    async def _run(self, task: DownloadTask) -> None:
        task.writer_active = True
        task.status = "RECEIVING"
        async with task.condition:
            task.condition.notify_all()
        writer: PartWriter | None = None
        body_file = None
        body_tmp = task.directory / "body.tmp"
        try:
            timeout = httpx.Timeout(120.0, connect=15.0, read=120.0, write=30.0)
            async with httpx.AsyncClient(
                transport=self.transport, headers=[],
                follow_redirects=False, timeout=timeout, trust_env=False,
            ) as client:
                async with _stream_get(
                    client, task.url, task.request_headers
                ) as response:
                    task.status_code = response.status_code
                    task.response_headers = filter_hop_headers(list(response.headers.multi_items()))
                    raw_length = response.headers.get("content-length")
                    if raw_length is not None:
                        try:
                            task.expected_size = int(raw_length)
                            if task.expected_size < 0:
                                raise ValueError
                        except ValueError:
                            raise ValueError("R2 返回了无效的 Content-Length")

                    bodyless = response.status_code in (204, 304) or 100 <= response.status_code < 200
                    known_large = (
                        not bodyless
                        and task.expected_size is not None
                        and task.expected_size > self.settings.split_threshold_bytes
                    )
                    if known_large:
                        task.mode = "parts"
                        writer = PartWriter(self, task)
                    else:
                        task.mode = "small"
                        body_file = body_tmp.open("xb")

                    if not bodyless:
                        async for chunk in _raw_chunks(response):
                            if not chunk:
                                continue
                            if writer is not None:
                                await writer.write(chunk)
                                continue

                            if task.total_size + len(chunk) <= self.settings.split_threshold_bytes:
                                await self.reserve(task, len(chunk))
                                await asyncio.to_thread(body_file.write, chunk)
                                task.total_size += len(chunk)
                                continue

                            # 未知长度或错误声明长度的响应越过阈值：
                            # 将有界 spool 前缀转入 Part writer，再继续消费上游流。
                            body_file.flush()
                            body_file.close()
                            body_file = None
                            task.mode = "parts"
                            writer = PartWriter(self, task)
                            task.total_size = 0
                            with body_tmp.open("rb") as prefix:
                                while True:
                                    prefix_chunk = await asyncio.to_thread(
                                        prefix.read, 1024 * 1024
                                    )
                                    if not prefix_chunk:
                                        break
                                    await writer.write(prefix_chunk, reserve=False)
                            body_tmp.unlink(missing_ok=True)
                            await writer.write(chunk)

                    if writer is not None:
                        await writer.finish()
                    elif body_file is not None:
                        await asyncio.to_thread(body_file.flush)
                        await asyncio.to_thread(os.fsync, body_file.fileno())
                        body_file.close()
                        body_file = None
                        os.replace(body_tmp, task.directory / "body.bin")

                    if (
                        not bodyless
                        and task.expected_size is not None
                        and task.expected_size != task.total_size
                    ):
                        raise ValueError(
                            f"R2 响应长度不匹配：声明 {task.expected_size}，实际 {task.total_size}"
                        )

            task.status = "COMPLETE"
            task.last_access = self.clock()
            task.expires_at = task.last_access + self.settings.cache_ttl_seconds
        except asyncio.CancelledError:
            task.status = "CANCELLED"
            task.error = {"type": "Cancelled", "message": "下载任务已取消"}
            raise
        except Exception as exc:
            task.status = "FAILED"
            # HTTP client exception strings may include a signed URL.
            task.error = {
                "type": type(exc).__name__,
                "message": "R2 下载失败；底层异常详情未向客户端公开",
            }
            task.expires_at = self.clock()
        finally:
            if body_file is not None:
                body_file.close()
            if writer is not None and writer.file is not None:
                await writer.abort()
            task.writer_active = False
            async with task.condition:
                task.condition.notify_all()
            if task.status in {"FAILED", "CANCELLED"}:
                task.delete_requested = True
            if task.delete_requested:
                await self._remove_files(task)

    def manifest(self, task: DownloadTask) -> dict:
        return {
            "task_id": task.task_id,
            "request_id": task.request_id,
            "status": task.status,
            "mode": task.mode,
            "status_code": task.status_code,
            "headers": [[key, value] for key, value in task.response_headers],
            "total_size": task.total_size,
            "expected_size": task.expected_size,
            "part_size_bytes": self.settings.part_size_bytes,
            "part_count": len(task.parts),
            "ready_part_count": sum(part["status"] == "READY" for part in task.parts),
            "parts": [dict(part) for part in task.parts],
            "error": task.error,
            "expires_at": task.expires_at,
        }

    def get(self, task_id: str) -> DownloadTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise DownloadNotFound("下载任务不存在或已过期")
        return task

    async def _wait(self, task: DownloadTask, predicate, *, part_number: int | None = None) -> None:
        async with task.condition:
            if predicate():
                if task.status in {"FAILED", "CANCELLED"}:
                    raise DownloadFailed("R2 上游下载失败或已取消")
                return
            # 全局等待者上限跨任务共享，必须在管理器锁下预留名额。
            async with self.lock:
                if self.waiter_count >= self.settings.max_waiters:
                    raise DownloadCapacity("VPS Part 等待连接数已达上限")
                self.waiter_count += 1
                task.waiters += 1
            try:
                await asyncio.wait_for(
                    task.condition.wait_for(predicate),
                    timeout=self.settings.wait_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise DownloadWaitTimeout("等待 Part 就绪超时") from exc
            finally:
                async with self.lock:
                    self.waiter_count -= 1
                task.waiters -= 1
        if task.status in {"FAILED", "CANCELLED"}:
            raise DownloadFailed("R2 上游下载失败或已取消")

    async def acquire_part(self, task_id: str, part_number: int) -> tuple[DownloadTask, Path, dict]:
        task = self.get(task_id)
        if part_number < 1:
            raise DownloadNotFound("Part 编号必须从 1 开始")

        def ready() -> bool:
            return any(p["part_number"] == part_number and p["status"] == "READY"
                       for p in task.parts) or task.status in {"COMPLETE", "FAILED", "CANCELLED"}

        await self._wait(task, ready, part_number=part_number)
        async with self.lock:
            if self.tasks.get(task_id) is not task or task.delete_requested:
                raise DownloadNotFound("下载任务不存在或已删除")
            part = next((p for p in task.parts
                         if p["part_number"] == part_number and p["status"] == "READY"), None)
            if task.status in {"FAILED", "CANCELLED"}:
                raise DownloadFailed("R2 上游下载失败或已取消")
            if part is None:
                raise DownloadNotFound("请求的 Part 编号不存在")
            path = task.directory / f"{part_number}.part"
            if not path.is_file():
                raise DownloadNotFound("Part 文件不存在或已过期")
            task.active_readers += 1
            task.last_access = self.clock()
            if task.status == "COMPLETE":
                task.expires_at = task.last_access + self.settings.cache_ttl_seconds
            return task, path, dict(part)

    async def acquire_body(self, task_id: str) -> tuple[DownloadTask, Path]:
        task = self.get(task_id)
        await self._wait(task, lambda: task.status in {"COMPLETE", "FAILED", "CANCELLED"})
        async with self.lock:
            if self.tasks.get(task_id) is not task or task.delete_requested:
                raise DownloadNotFound("下载任务不存在或已删除")
            if task.status != "COMPLETE":
                raise DownloadFailed("R2 上游下载失败或已取消")
            if task.mode != "small":
                raise DownloadConflict("该任务为分片响应，请按 Part 编号取回")
            path = task.directory / "body.bin"
            if not path.is_file():
                raise DownloadNotFound("小响应文件不存在或已过期")
            task.active_readers += 1
            task.last_access = self.clock()
            task.expires_at = task.last_access + self.settings.cache_ttl_seconds
            return task, path

    async def reader_finished(self, task: DownloadTask) -> None:
        async with self.lock:
            task.active_readers = max(0, task.active_readers - 1)
            task.last_access = self.clock()
            if task.status == "COMPLETE" and not task.delete_requested:
                task.expires_at = task.last_access + self.settings.cache_ttl_seconds
            if task.delete_requested and not task.writer_active and task.active_readers == 0:
                self._remove_files_locked(task)

    async def stream_file(self, task: DownloadTask, path: Path) -> AsyncIterator[bytes]:
        try:
            with path.open("rb") as stream:
                while True:
                    chunk = await asyncio.to_thread(stream.read, 1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            await self.reader_finished(task)

    def _remove_files_locked(self, task: DownloadTask) -> None:
        if task.writer_active or task.active_readers:
            return
        shutil.rmtree(task.directory, ignore_errors=True)
        self.used_bytes = max(0, self.used_bytes - task.stored_bytes)
        task.stored_bytes = 0
        task.parts.clear()

    async def _remove_files(self, task: DownloadTask) -> None:
        async with self.lock:
            self._remove_files_locked(task)

    async def delete(self, task_id: str) -> None:
        async with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return
            task.delete_requested = True
            self.tasks.pop(task_id, None)
            self.request_index.pop(task.request_id, None)
            worker = task.worker
            if not task.writer_active and task.active_readers == 0:
                self._remove_files_locked(task)
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        async with self.lock:
            if not task.writer_active and task.active_readers == 0:
                self._remove_files_locked(task)

    async def cleanup(self) -> int:
        now = self.clock()
        removed = 0
        async with self.lock:
            expired = [
                task for task in tuple(self.tasks.values())
                if task.expires_at is not None
                and now >= task.expires_at
                and not task.writer_active
                and task.active_readers == 0
                and task.waiters == 0
            ]
            for task in expired:
                self._remove_files_locked(task)
                self.tasks.pop(task.task_id, None)
                self.request_index.pop(task.request_id, None)
                removed += 1
        return removed

    async def close(self) -> None:
        # Stop every upstream writer before releasing the exclusive directory
        # lock; otherwise a second worker could erase files still being written.
        async with self.lock:
            tasks = tuple(self.tasks.values())
            for task in tasks:
                task.delete_requested = True
            workers = [task.worker for task in tasks
                       if task.worker is not None and not task.worker.done()]
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

        # The ASGI server should drain active responses before lifespan shutdown.
        # Keep the process lock held if a reader fails to close in time.
        deadline = asyncio.get_running_loop().time() + 30.0
        while any(task.active_readers for task in tasks):
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    "VPS 下载仍有活动文件读取；为避免跨进程删除，保留独占目录锁"
                )
            await asyncio.sleep(0.05)

        async with self.lock:
            for task in tasks:
                self._remove_files_locked(task)
                self.tasks.pop(task.task_id, None)
                self.request_index.pop(task.request_id, None)
            if self._process_lock is not None:
                try:
                    fcntl.flock(self._process_lock.fileno(), fcntl.LOCK_UN)
                finally:
                    self._process_lock.close()
                    self._process_lock = None

    def metrics(self) -> dict:
        return {
            "enabled": self.settings.enabled,
            "task_count": len(self.tasks),
            "active_task_count": sum(
                task.status in {"QUEUED", "RECEIVING"} for task in self.tasks.values()
            ),
            "retained_task_limit": self.settings.max_retained_tasks,
            "ready_part_count": sum(
                part["status"] == "READY"
                for task in self.tasks.values() for part in task.parts
            ),
            "cached_bytes": self.used_bytes,
            "active_readers": sum(task.active_readers for task in self.tasks.values()),
            "waiters": self.waiter_count,
        }

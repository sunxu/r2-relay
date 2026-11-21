"""Mac 端 R2 GET 下载协调器：并发取回 READY Part，写入单一任务文件。"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

import httpx

from .config import Settings
from .relay import DownloadPartTransportError


def _pwrite_all(fd: int, data: bytes, offset: int) -> None:
    """Write every byte at the requested offset or fail without claiming success."""
    view = memoryview(data)
    written = 0
    while written < len(view):
        try:
            count = os.pwrite(fd, view[written:], offset + written)
        except InterruptedError:
            continue
        if count <= 0:
            raise OSError("pwrite made no progress")
        written += count


async def _run_disk_operation(function, *args):
    """Run blocking disk I/O without letting task cancellation outlive the worker.

    asyncio.to_thread() cannot stop a running OS thread. Shield its task and,
    after cancellation, wait until the worker has really finished before
    propagating CancelledError to the caller. Callers may then safely close
    the file descriptor or remove its backing file.
    """
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            # Retrieve the worker exception below. If cancellation was also
            # requested, cancellation remains the caller-visible outcome.
            break
    try:
        result = worker.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result


class DownloadRelayError(Exception):
    """下载中继失败；不得静默回退为直连 R2。"""


class DownloadCapacityError(DownloadRelayError):
    """本地内存或临时磁盘预算不足。"""


@dataclass
class DownloadResult:
    status_code: int
    headers: list[tuple[str, str]]
    size: int
    body: bytes | bytearray | None = None
    path: Path | None = None
    cleanup: Callable[[], Awaitable[None]] | None = None
    stream: AsyncIterator[bytes] | None = None


@dataclass
class _PartStreamProgress:
    """A Part is exposed only after Mac length and SHA-256 verification."""
    path: Path | None = None
    ready: dict[int, int] | None = None
    changed: asyncio.Event | None = None

    def __post_init__(self) -> None:
        self.ready = {}
        self.changed = asyncio.Event()

    def publish(self, number: int, size: int) -> None:
        self.ready[number] = size
        self.changed.set()


class WeightedBudget:
    """跨所有下载任务共享的字节预算；容量不足时排队等待。"""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self.condition = asyncio.Condition()

    async def acquire(self, amount: int) -> None:
        if amount < 0 or amount > self.limit:
            raise DownloadCapacityError("单个响应超过本地资源预算")
        async with self.condition:
            await self.condition.wait_for(lambda: self.used + amount <= self.limit)
            self.used += amount

    async def release(self, amount: int) -> None:
        async with self.condition:
            self.used = max(0, self.used - amount)
            self.condition.notify_all()


class DownloadCoordinator:
    def __init__(self, relay, settings: Settings):
        self.relay = relay
        self.settings = settings
        self.global_semaphore = asyncio.Semaphore(settings.download_global_max_concurrency)
        self.memory_budget = WeightedBudget(settings.download_small_memory_bytes)
        self.disk_budget = WeightedBudget(settings.download_max_temp_bytes)
        self.root = settings.download_temp_dir
        self.active: set[asyncio.Task] = set()
        self.remote_cleanup_failures = 0
        self._closed = False
        self._process_lock = None
        if settings.download_enabled:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink():
                raise RuntimeError("下载临时目录不得是符号链接")
            os.chmod(self.root, 0o700)
            lock_path = self.root / ".process.lock"
            self._process_lock = lock_path.open("a+b")
            try:
                import fcntl
                fcntl.flock(self._process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self._process_lock.close()
                self._process_lock = None
                raise RuntimeError("下载临时目录已被另一个本地 Relay 进程使用") from exc
            # 清理上次异常退出遗留的临时对象；进程锁保证不会碰到活动 writer。
            for child in self.root.iterdir():
                if child.name == ".process.lock":
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                elif child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)

    async def _delete_remote(self, task_id: str) -> bool:
        try:
            await self.relay.download_delete(task_id)
            return True
        except Exception:
            # VPS 600 秒空闲 TTL 是清理兜底；记录失败供验收与指标使用。
            self.remote_cleanup_failures += 1
            return False

    async def _download_small(self, task_id: str, manifest: dict) -> DownloadResult:
        size = int(manifest["total_size"])
        if size > 24 * 1024 * 1024:
            raise DownloadRelayError("VPS 将大响应错误标记为 small 模式")
        await asyncio.wait_for(
            self.memory_budget.acquire(size),
            timeout=self.relay.execution_timeout,
        )
        try:
            async with self.global_semaphore:
                body = await self.relay.download_body(task_id, max_bytes=24 * 1024 * 1024)
            if len(body) != size:
                raise DownloadRelayError("小响应实际长度与 manifest 不一致")
            status = int(manifest["status_code"])
            if status not in (204, 304) and not 100 <= status < 200:
                expected = manifest.get("expected_size")
                if expected is not None and int(expected) != len(body):
                    raise DownloadRelayError("小响应声明长度与实际长度不一致")
            await self._delete_remote(task_id)
            released = False

            async def cleanup() -> None:
                nonlocal released
                if not released:
                    released = True
                    body.clear()
                    await self.memory_budget.release(size)

            return DownloadResult(
                status_code=status,
                headers=[(str(k), str(v)) for k, v in manifest["headers"]],
                size=size,
                body=body,
                cleanup=cleanup,
            )
        except BaseException:
            await self.memory_budget.release(size)
            raise

    async def _fetch_one_part(
        self,
        task_id: str,
        part: dict,
        fd: int,
        part_size_bytes: int,
        per_file_semaphore: asyncio.Semaphore,
        held_parts: dict[int, int],
        progress: _PartStreamProgress | None = None,
    ) -> None:
        number = int(part["part_number"])
        size = int(part["size"])
        expected_hash = str(part["sha256"])
        offset = (number - 1) * part_size_bytes
        if number < 1 or size <= 0 or size > part_size_bytes:
            raise DownloadRelayError("VPS manifest 中的 Part 尺寸无效")
        if shutil.disk_usage(self.root).free < size:
            raise DownloadCapacityError("Mac 临时磁盘可用空间不足")

        last_error: Exception | None = None
        for attempt in range(3):
            digest = hashlib.sha256()
            received = 0
            try:
                async with self.global_semaphore, per_file_semaphore:
                    async with self.relay.download_part_stream(task_id, number) as response:
                        if response.status_code != 200:
                            raise DownloadRelayError(
                                f"VPS Part API 返回 HTTP {response.status_code}"
                            )
                        raw_size = response.headers.get("content-length")
                        if raw_size is not None and int(raw_size) != size:
                            raise DownloadRelayError("Part HTTP Content-Length 与 manifest 不一致")
                        if response.headers.get("x-part-sha256") != expected_hash:
                            raise DownloadRelayError("Part HTTP SHA-256 与 manifest 不一致")
                        if response.headers.get("x-part-number") != str(number):
                            raise DownloadRelayError("VPS 返回了错误的 Part 编号")
                        async for chunk in response.aiter_raw():
                            if not chunk:
                                continue
                            if received + len(chunk) > size:
                                raise DownloadRelayError("Part 响应超过 manifest 声明长度")
                            await _run_disk_operation(_pwrite_all, fd, chunk, offset + received)
                            digest.update(chunk)
                            received += len(chunk)
                if received != size:
                    raise DownloadRelayError("Part 响应未完整传输")
                if digest.hexdigest() != expected_hash:
                    raise DownloadRelayError("Part SHA-256 校验失败")
                held_parts[number] = size
                if progress is not None:
                    progress.publish(number, size)
                return
            except (
                httpx.TransportError,
                httpx.TimeoutException,
                DownloadPartTransportError,
                DownloadRelayError,
                ValueError,
            ) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep((0.15, 0.5)[attempt])
        raise DownloadRelayError(
            f"Part {number} 在 3 次尝试后仍未成功：{type(last_error).__name__ if last_error else 'Unknown'}"
        )

    async def _download_large(
        self, task_id: str, manifest: dict, *, deadline: float,
        progress: _PartStreamProgress | None = None,
    ) -> DownloadResult:
        fd, raw_path = tempfile.mkstemp(prefix="r2-download-", suffix=".bin", dir=self.root)
        path = Path(raw_path)
        os.fchmod(fd, 0o600)
        if progress is not None:
            progress.path = path
        held_parts: dict[int, int] = {}
        inflight: dict[int, asyncio.Task] = {}
        fetched: set[int] = set()
        per_file_semaphore = asyncio.Semaphore(self.settings.download_max_concurrency)
        succeeded = False
        reserved_total = 0
        try:
            while True:
                if asyncio.get_running_loop().time() >= deadline:
                    raise DownloadRelayError("等待 VPS 分片和本地取回超时")
                manifest = await self.relay.download_manifest(task_id)
                status = manifest["status"]
                if status in {"FAILED", "CANCELLED"}:
                    raise DownloadRelayError("VPS 上游 GET 任务失败或已取消")
                if manifest.get("mode") not in (None, "parts"):
                    raise DownloadRelayError("VPS manifest 响应模式在下载期间发生变化")

                expected_size = manifest.get("expected_size")
                observed_size = int(manifest.get("total_size", 0))
                if expected_size is not None:
                    expected_size = int(expected_size)
                    if expected_size > self.settings.download_max_temp_bytes:
                        raise DownloadCapacityError("大响应超过 Mac 临时磁盘预算")
                    if observed_size > expected_size:
                        raise DownloadRelayError("VPS 实际响应长度超过声明长度")
                    if reserved_total == 0:
                        if shutil.disk_usage(self.root).free < expected_size:
                            raise DownloadCapacityError("Mac 临时磁盘可用空间不足")
                        remaining = deadline - asyncio.get_running_loop().time()
                        await asyncio.wait_for(
                            self.disk_budget.acquire(expected_size),
                            timeout=max(0.001, remaining),
                        )
                        reserved_total = expected_size
                elif observed_size > self.settings.download_max_temp_bytes:
                    raise DownloadCapacityError("未知长度响应超过 Mac 临时磁盘预算")

                # 未知长度响应先等 VPS COMPLETE，确定总长度后一次性预留，
                # 避免多个未知长度任务各自占用部分预算而互相等待。
                can_fetch = expected_size is not None or status == "COMPLETE"
                if can_fetch and status == "COMPLETE" and reserved_total == 0:
                    total_size_for_reserve = int(manifest["total_size"])
                    if total_size_for_reserve > self.settings.download_max_temp_bytes:
                        raise DownloadCapacityError("大响应超过 Mac 临时磁盘预算")
                    if shutil.disk_usage(self.root).free < total_size_for_reserve:
                        raise DownloadCapacityError("Mac 临时磁盘可用空间不足")
                    remaining = deadline - asyncio.get_running_loop().time()
                    await asyncio.wait_for(
                        self.disk_budget.acquire(total_size_for_reserve),
                        timeout=max(0.001, remaining),
                    )
                    reserved_total = total_size_for_reserve

                if can_fetch:
                    for part in manifest.get("parts", []):
                        number = int(part["part_number"])
                        if (
                            part.get("status") == "READY"
                            and number not in fetched
                            and number not in inflight
                            and len(inflight) < self.settings.download_max_concurrency
                        ):
                            inflight[number] = asyncio.create_task(
                                self._fetch_one_part(
                                    task_id, part, fd,
                                    int(manifest["part_size_bytes"]),
                                    per_file_semaphore, held_parts, progress,
                                )
                            )

                for number, task in tuple(inflight.items()):
                    if task.done():
                        await task
                        fetched.add(number)
                        inflight.pop(number, None)

                if status == "COMPLETE":
                    parts = sorted(manifest.get("parts", []), key=lambda p: int(p["part_number"]))
                    if int(manifest.get("ready_part_count", -1)) != len(parts):
                        raise DownloadRelayError("VPS 任务 COMPLETE，但仍有 Part 未 READY")
                    if inflight:
                        remaining = deadline - asyncio.get_running_loop().time()
                        await asyncio.wait_for(
                            asyncio.gather(*inflight.values()),
                            timeout=max(0.001, remaining),
                        )
                        fetched.update(inflight)
                        inflight.clear()
                    if len(fetched) != len(parts):
                        # READY Part 数超过本轮并发窗口时，继续下一轮调度。
                        continue
                    part_size = int(manifest["part_size_bytes"])
                    total_size = int(manifest["total_size"])
                    if sum(int(p["size"]) for p in parts) != total_size:
                        raise DownloadRelayError("Part 总长度与 VPS manifest 不一致")
                    if int(manifest.get("part_count", -1)) != len(parts):
                        raise DownloadRelayError("VPS manifest Part 数量不一致")
                    for index, part in enumerate(parts):
                        number = index + 1
                        expected_size = part_size if index < len(parts) - 1 else total_size - index * part_size
                        if int(part["part_number"]) != number or int(part["size"]) != expected_size:
                            raise DownloadRelayError("VPS Part 编号、偏移或长度不连续")
                        if part.get("status") != "READY" or number not in held_parts:
                            raise DownloadRelayError("VPS COMPLETE 后仍有 Part 未完成本地校验")
                    expected_size = manifest.get("expected_size")
                    if expected_size is not None and int(expected_size) != total_size:
                        raise DownloadRelayError("大响应长度与 Content-Length 不一致")
                    await _run_disk_operation(os.ftruncate, fd, total_size)
                    await _run_disk_operation(os.fsync, fd)
                    os.close(fd)
                    fd = -1
                    await self._delete_remote(task_id)
                    succeeded = True
                    released = False

                    async def cleanup() -> None:
                        nonlocal released
                        if released:
                            return
                        released = True
                        path.unlink(missing_ok=True)
                        await self.disk_budget.release(reserved_total)

                    return DownloadResult(
                        status_code=int(manifest["status_code"]),
                        headers=[(str(k), str(v)) for k, v in manifest["headers"]],
                        size=total_size,
                        path=path,
                        cleanup=cleanup,
                    )

                await asyncio.sleep(self.relay.poll_interval)

        finally:
            for task in inflight.values():
                if not task.done():
                    task.cancel()
            if inflight:
                await asyncio.gather(*inflight.values(), return_exceptions=True)
            if fd >= 0:
                os.close(fd)
            if not succeeded:
                path.unlink(missing_ok=True)
                if reserved_total:
                    await self.disk_budget.release(reserved_total)

    async def _complete_task(
        self, task_id: str, manifest: dict | None, deadline: float,
        progress: _PartStreamProgress | None = None,
    ) -> DownloadResult:
        """Continue an existing single VPS task; never create a second R2 GET."""
        while asyncio.get_running_loop().time() < deadline:
            if manifest is None:
                manifest = await self.relay.download_manifest(task_id)
            status = manifest["status"]
            if status in {"FAILED", "CANCELLED"}:
                raise DownloadRelayError("VPS 上游 GET 任务失败或已取消")
            mode = manifest.get("mode")
            if mode == "small" and status == "COMPLETE":
                return await self._download_small(task_id, manifest)
            if mode == "parts":
                return await self._download_large(
                    task_id, manifest, deadline=deadline, progress=progress,
                )
            await asyncio.sleep(self.relay.poll_interval)
            manifest = None
        raise DownloadRelayError("等待 VPS R2 GET 任务超时")

    async def _iter_verified_parts(
        self, worker: asyncio.Task, progress: _PartStreamProgress,
        total_size: int, part_size: int,
    ) -> AsyncIterator[bytes]:
        """Only expose completely verified Parts, in response-body order."""
        next_part = 1
        sent = 0
        while sent < total_size:
            # A failed writer invalidates even already prefetched later parts.
            if worker.done():
                worker.result()  # Raises the original upstream/validation error.
            ready_size = progress.ready.get(next_part)
            if ready_size is None:
                if worker.done():
                    worker.result()
                    raise DownloadRelayError("响应已结束但缺少预期 READY Part")
                progress.changed.clear()
                if next_part not in progress.ready and not worker.done():
                    await progress.changed.wait()
                continue
            if ready_size <= 0 or ready_size > total_size - sent or progress.path is None:
                raise DownloadRelayError("READY Part 的长度或文件位置无效")
            with progress.path.open("rb", buffering=0) as source:
                await asyncio.to_thread(source.seek, (next_part - 1) * part_size)
                remaining = ready_size
                while remaining:
                    chunk = await asyncio.to_thread(source.read, min(1024 * 1024, remaining))
                    if not chunk:
                        raise DownloadRelayError("Mac 已验证 Part 在交付前发生截断")
                    yield chunk
                    remaining -= len(chunk)
            sent += ready_size
            next_part += 1
        complete = await worker
        if complete.size != sent:
            raise DownloadRelayError("最终响应长度与已交付 Part 长度不匹配")

    async def download_stream(
        self, url: str, headers: list[tuple[str, str]],
    ) -> DownloadResult:
        """Return real R2 headers while its body is still downloading.

        Large known-length bodies use checked READY Parts; other bodies retain
        the bounded v1 completion path, but no longer delay response headers.
        """
        if not self.settings.download_enabled or self._closed:
            raise DownloadRelayError("本地 R2 GET 下载中继未启用或正在关闭")
        current = asyncio.current_task()
        if current is not None:
            self.active.add(current)
        task_id: str | None = None
        worker: asyncio.Task | None = None
        try:
            created = await self.relay.download_create(secrets.token_urlsafe(18), url, headers)
            task_id = created["task_id"]
            deadline = asyncio.get_running_loop().time() + self.relay.execution_timeout
            manifest: dict | None = None
            while asyncio.get_running_loop().time() < deadline:
                manifest = await self.relay.download_manifest(task_id)
                if manifest["status"] in {"FAILED", "CANCELLED"}:
                    raise DownloadRelayError("R2 响应头尚未交付时 VPS 任务已失败")
                if manifest.get("status_code") is not None:
                    break
                await asyncio.sleep(self.relay.poll_interval)
            else:
                raise DownloadRelayError("等待 R2 原始响应头超时")
            status = int(manifest["status_code"])
            original_headers = [(str(k), str(v)) for k, v in manifest["headers"]]
            expected = manifest.get("expected_size")
            expected = int(expected) if expected is not None else None
            if expected is not None and expected < 0:
                raise DownloadRelayError("R2 原始 Content-Length 无效")
            large = (
                expected is not None
                and expected > 24 * 1024 * 1024
            )
            if large and (
                expected > self.settings.download_max_temp_bytes
                or shutil.disk_usage(self.root).free < expected
            ):
                raise DownloadCapacityError("下载响应超过 Mac 临时磁盘容量")
            if expected is not None and not large and (
                expected > self.settings.download_small_memory_bytes
            ):
                # No body has been committed yet: fail before sending success headers.
                raise DownloadCapacityError("下载响应超过 Mac 小响应内存预算")
            progress = _PartStreamProgress() if large else None
            worker = asyncio.create_task(self._complete_task(task_id, manifest, deadline, progress))
            self.active.add(worker)
            worker.add_done_callback(self.active.discard)
            if progress is not None:
                worker.add_done_callback(lambda _: progress.changed.set())
            cleaned = False

            async def cleanup() -> None:
                nonlocal cleaned
                if cleaned:
                    return
                cleaned = True
                if not worker.done():
                    worker.cancel()
                outcome = (await asyncio.gather(worker, return_exceptions=True))[0]
                if isinstance(outcome, DownloadResult):
                    if outcome.cleanup is not None:
                        await outcome.cleanup()
                else:
                    await self._delete_remote(task_id)

            async def body_stream() -> AsyncIterator[bytes]:
                try:
                    if progress is not None:
                        async for chunk in self._iter_verified_parts(
                            worker, progress, expected, int(manifest["part_size_bytes"]),
                        ):
                            yield chunk
                    else:
                        completed = await worker
                        if completed.body is not None:
                            for offset in range(0, len(completed.body), 1024 * 1024):
                                yield bytes(completed.body[offset:offset + 1024 * 1024])
                        elif completed.path is not None:
                            with completed.path.open("rb", buffering=0) as source:
                                while True:
                                    chunk = await asyncio.to_thread(source.read, 1024 * 1024)
                                    if not chunk:
                                        break
                                    yield chunk
                finally:
                    await cleanup()

            return DownloadResult(
                status_code=status,
                headers=original_headers,
                size=expected if expected is not None else -1,
                stream=body_stream(),
                cleanup=cleanup,
            )
        except BaseException:
            if task_id is not None and worker is None:
                await self._delete_remote(task_id)
            raise
        finally:
            if current is not None:
                self.active.discard(current)

    async def download(self, url: str, headers: list[tuple[str, str]]) -> DownloadResult:
        if not self.settings.download_enabled:
            raise DownloadRelayError("本地 R2 GET 下载中继未启用")
        if self._closed:
            raise DownloadRelayError("本地 R2 GET 下载中继正在关闭")
        current = asyncio.current_task()
        if current is not None:
            self.active.add(current)
        task_id: str | None = None
        try:
            request_id = secrets.token_urlsafe(18)
            created = await self.relay.download_create(request_id, url, headers)
            task_id = created["task_id"]
            deadline = asyncio.get_running_loop().time() + self.relay.execution_timeout
            return await self._complete_task(task_id, None, deadline)
        except asyncio.CancelledError:
            if task_id is not None:
                await self._delete_remote(task_id)
            raise
        except Exception:
            if task_id is not None:
                await self._delete_remote(task_id)
            raise
        finally:
            if current is not None:
                self.active.discard(current)

    async def close(self) -> None:
        self._closed = True
        active = [task for task in self.active if task is not asyncio.current_task()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        if self._process_lock is not None:
            try:
                import fcntl
                fcntl.flock(self._process_lock.fileno(), fcntl.LOCK_UN)
            finally:
                self._process_lock.close()
                self._process_lock = None

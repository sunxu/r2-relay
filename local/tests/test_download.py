import asyncio
import hashlib
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mitmproxy import http

from r2relay_local.addon import R2RelayAddon
from r2relay_local.config import Settings
from r2relay_local.disk_response import DiskResponseServer
from r2relay_local import download as download_module
from r2relay_local.download import DownloadCapacityError, DownloadCoordinator, DownloadResult
from r2relay_local.relay import DownloadPartTransportError, RelayClient, is_r2_get
from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore
from r2relay_vps.config import DownloadSettings as VPSDownloadSettings


MIB = 1024 * 1024
R2_URL = "https://abc.r2.cloudflarestorage.com/bucket/object?version=1"


def local_settings(tmp_path, **overrides):
    values = dict(
        max_part_bytes=128 * MIB,
        global_budget_bytes=1024 * MIB,
        source=tmp_path / "config.toml",
        download_enabled=True,
        download_max_concurrency=4,
        download_global_max_concurrency=8,
        download_small_memory_bytes=24 * MIB,
        download_max_temp_bytes=64 * MIB,
        download_temp_dir=tmp_path / "local-downloads",
    )
    values.update(overrides)
    return Settings(**values)


def vps_settings():
    return VPSDownloadSettings(
        enabled=True,
        part_size_bytes=16 * MIB,
        split_threshold_bytes=24 * MIB,
        cache_ttl_seconds=600,
        max_cache_bytes=128 * MIB,
        min_free_bytes=1,
        max_active_tasks=4,
        max_retained_tasks=16,
        max_waiters=16,
        wait_timeout_seconds=5,
    )


def make_vps(tmp_path, body, *, status=200, headers=None):
    def upstream(request):
        assert request.method == "GET"
        return httpx.Response(status, content=body, headers=headers or {})

    app = create_app(
        CacheStore(tmp_path / "upload-cache"),
        token="download-test-token",
        download_transport=httpx.MockTransport(upstream),
        download_settings=vps_settings(),
        download_root=tmp_path / "vps-downloads",
    )
    return app


@pytest.mark.parametrize(
    ("method", "url", "expected"),
    [
        ("GET", R2_URL, True),
        ("get", R2_URL, True),
        ("GET", "https://abc.r2.cloudflarestorage.com:443/b/k", True),
        ("GET", "http://abc.r2.cloudflarestorage.com/b/k", False),
        ("GET", "https://abc.r2.cloudflarestorage.com:8443/b/k", False),
        ("GET", "https://example.com/b/k", False),
        ("PUT", R2_URL, False),
    ],
)
def test_r2_get_target_matching(method, url, expected):
    assert is_r2_get(method, url) is expected


@pytest.mark.asyncio
async def test_mac_download_small_response_uses_bounded_memory_and_preserves_r2_status(tmp_path):
    body = b"small response body"
    app = make_vps(tmp_path, body, status=403, headers={"ETag": '"r2-error"'})
    transport = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://vps.test"
    )
    relay = RelayClient(
        "https://vps.test", "download-test-token",
        client=transport, execution_timeout=10, poll_interval=0.002,
    )
    coordinator = DownloadCoordinator(relay, local_settings(tmp_path))
    try:
        result = await coordinator.download(R2_URL, [("Range", "bytes=0-18")])
        assert result.status_code == 403
        assert result.body == body
        assert isinstance(result.body, bytearray)
        assert result.path is None
        assert ("etag", '"r2-error"') in [(k.lower(), v) for k, v in result.headers]
        assert coordinator.memory_budget.used == len(body)
        await result.cleanup()
        assert result.body == bytearray()
        assert coordinator.memory_budget.used == 0
        assert not app.state.downloads.tasks
    finally:
        await coordinator.close()
        await transport.aclose()


@pytest.mark.asyncio
async def test_mac_download_large_response_fetches_ready_parts_into_one_file(tmp_path):
    body = bytes((i % 251 for i in range(25 * MIB)))
    app = make_vps(tmp_path, body, status=206, headers={
        "ETag": '"object-version-1"',
        "Content-Range": f"bytes 0-{len(body)-1}/{len(body)}",
    })
    transport = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://vps.test"
    )
    relay = RelayClient(
        "https://vps.test", "download-test-token",
        client=transport, execution_timeout=10, poll_interval=0.002,
    )
    coordinator = DownloadCoordinator(relay, local_settings(tmp_path))
    try:
        result = await coordinator.download(
            R2_URL, [("Range", f"bytes=0-{len(body)-1}")]
        )
        assert result.status_code == 206
        assert result.body is None
        assert result.path is not None
        assert result.size == len(body)
        assert result.path.stat().st_size == len(body)
        assert result.path.read_bytes() == body
        assert coordinator.disk_budget.used == len(body)
        assert ("content-range", f"bytes 0-{len(body)-1}/{len(body)}") in [
            (k.lower(), v) for k, v in result.headers
        ]
        # Only one task-level file exists; no local N.part files are retained.
        assert not list(coordinator.root.glob("*.part"))
        await result.cleanup()
        assert not result.path.exists()
        assert coordinator.disk_budget.used == 0
        assert not app.state.downloads.tasks
    finally:
        await coordinator.close()
        await transport.aclose()


@pytest.mark.asyncio
async def test_loopback_response_server_streams_file_and_releases_it_on_disconnect(tmp_path):
    source = tmp_path / "response.bin"
    source.write_bytes(b"disk-backed-response")
    released = asyncio.Event()

    async def cleanup():
        source.unlink(missing_ok=True)
        released.set()

    result = DownloadResult(
        status_code=206,
        headers=[
            ("Content-Range", "bytes 10-29/100"),
            ("ETag", '"range-etag"'),
            ("Content-Length", str(source.stat().st_size)),
        ],
        size=source.stat().st_size,
        path=source,
        cleanup=cleanup,
    )
    server = DiskResponseServer()
    token = await server.register(result)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(
            f"GET /download/{token} HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n\r\n".encode()
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        head, body = response.split(b"\r\n\r\n", 1)
        assert head.startswith(b"HTTP/1.1 206 Partial Content")
        assert b"Content-Range: bytes 10-29/100" in head
        assert b"Content-Length: 20" in head
        assert body == b"disk-backed-response"
        await asyncio.wait_for(released.wait(), timeout=2)
        assert not source.exists()
    finally:
        await server.close()


class FakeDownloads:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.closed = False

    async def download(self, url, headers):
        self.calls.append((url, headers))
        return self.result

    async def close(self):
        self.closed = True


class FakeRelay:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_addon_rewrites_r2_get_to_loopback_and_streams_verified_result(tmp_path):
    released = asyncio.Event()

    async def cleanup():
        released.set()

    result = DownloadResult(
        status_code=200,
        headers=[("ETag", '"downloaded"'), ("Content-Type", "application/octet-stream")],
        size=5,
        body=b"hello",
        cleanup=cleanup,
    )
    downloads = FakeDownloads(result)
    disk_server = DiskResponseServer()
    relay = FakeRelay()
    addon = R2RelayAddon(
        relay,
        settings=local_settings(tmp_path),
        download_coordinator=downloads,
        disk_response_server=disk_server,
    )
    request_headers = http.Headers()
    request_headers["Authorization"] = "AWS4-HMAC-SHA256 signed"
    request_headers["Range"] = "bytes=0-4"
    request_headers["Cookie"] = "must-not-go-to-loopback"
    request = SimpleNamespace(
        method="GET", pretty_url=R2_URL, headers=request_headers,
        scheme="https", host="abc.r2.cloudflarestorage.com", port=443,
        path="/bucket/object?version=1",
    )
    flow = SimpleNamespace(request=request, metadata={}, response=None)
    try:
        await addon.request(flow)
        assert downloads.calls
        original_url, original_headers = downloads.calls[0]
        assert original_url == R2_URL
        assert ("Range", "bytes=0-4") in original_headers
        assert ("Authorization", "AWS4-HMAC-SHA256 signed") in original_headers
        assert flow.request.scheme == "http"
        assert flow.request.host == "127.0.0.1"
        assert flow.request.port == disk_server.port
        assert flow.request.path.startswith("/download/")
        assert "Authorization" not in flow.request.headers
        assert "Cookie" not in flow.request.headers

        flow.response = http.Response.make(200, b"")
        addon.responseheaders(flow)
        assert flow.response.stream is True

        reader, writer = await asyncio.open_connection("127.0.0.1", disk_server.port)
        writer.write(
            f"GET {flow.request.path} HTTP/1.1\r\nHost: 127.0.0.1:{disk_server.port}\r\n\r\n".encode()
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        assert response.endswith(b"\r\n\r\nhello")
        await asyncio.wait_for(released.wait(), timeout=2)
    finally:
        await addon.done()
    assert downloads.closed
    assert relay.closed



class UnknownLengthStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_mac_unknown_length_download_waits_for_complete_then_assembles(tmp_path):
    body = b"u" * (25 * MIB)

    def upstream(_request):
        return httpx.Response(
            200,
            stream=UnknownLengthStream([body[:16 * MIB], body[16 * MIB:]]),
        )

    app = create_app(
        CacheStore(tmp_path / "upload-cache"),
        token="download-test-token",
        download_transport=httpx.MockTransport(upstream),
        download_settings=vps_settings(),
        download_root=tmp_path / "vps-downloads",
    )
    transport = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://vps.test"
    )
    relay = RelayClient(
        "https://vps.test", "download-test-token",
        client=transport, execution_timeout=10, poll_interval=0.002,
    )
    coordinator = DownloadCoordinator(relay, local_settings(tmp_path))
    try:
        result = await coordinator.download(R2_URL, [])
        assert result.path is not None
        assert result.size == len(body)
        assert result.path.read_bytes() == body
        await result.cleanup()
        assert coordinator.disk_budget.used == 0
    finally:
        await coordinator.close()
        await transport.aclose()


@pytest.mark.asyncio
async def test_mac_download_fails_closed_when_large_object_exceeds_temp_budget(tmp_path):
    body = b"z" * (25 * MIB)
    app = make_vps(tmp_path, body)
    transport = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://vps.test"
    )
    relay = RelayClient(
        "https://vps.test", "download-test-token",
        client=transport, execution_timeout=10, poll_interval=0.002,
    )
    coordinator = DownloadCoordinator(
        relay,
        local_settings(tmp_path, download_max_temp_bytes=24 * MIB),
    )
    try:
        with pytest.raises(DownloadCapacityError, match="超过 Mac 临时磁盘预算"):
            await coordinator.download(R2_URL, [])
        assert not app.state.downloads.tasks
        assert not list(coordinator.root.glob("r2-download-*.bin"))
        assert coordinator.disk_budget.used == 0
    finally:
        await coordinator.close()
        await transport.aclose()



class _FakePartResponse:
    status_code = 200

    def __init__(self, number, digest):
        self.headers = {
            "content-length": "1",
            "x-part-sha256": digest,
            "x-part-number": str(number),
        }

    async def aiter_raw(self):
        await asyncio.sleep(0.01)
        yield b"x"


class _ConcurrencyRelay:
    poll_interval = 0.001
    execution_timeout = 5

    def __init__(self, part_count):
        import hashlib

        digest = hashlib.sha256(b"x").hexdigest()
        self.manifests = {}
        for task_id in ("file-a", "file-b"):
            parts = [
                {"part_number": number, "size": 1, "sha256": digest, "status": "READY"}
                for number in range(1, part_count + 1)
            ]
            self.manifests[task_id] = {
                "task_id": task_id,
                "status": "COMPLETE",
                "mode": "parts",
                "status_code": 200,
                "headers": [],
                "total_size": part_count,
                "expected_size": part_count,
                "part_size_bytes": 1,
                "part_count": part_count,
                "ready_part_count": part_count,
                "parts": parts,
            }
        self.active = 0
        self.max_active = 0
        self.active_by_file = {"file-a": 0, "file-b": 0}
        self.max_by_file = {"file-a": 0, "file-b": 0}

    async def download_manifest(self, task_id):
        return self.manifests[task_id]

    @asynccontextmanager
    async def download_part_stream(self, task_id, number):
        import hashlib

        self.active += 1
        self.active_by_file[task_id] += 1
        self.max_active = max(self.max_active, self.active)
        self.max_by_file[task_id] = max(
            self.max_by_file[task_id], self.active_by_file[task_id]
        )
        try:
            yield _FakePartResponse(number, hashlib.sha256(b"x").hexdigest())
        finally:
            self.active -= 1
            self.active_by_file[task_id] -= 1

    async def download_delete(self, task_id):
        return None


@pytest.mark.asyncio
async def test_two_downloads_enforce_16_per_file_and_32_global_part_concurrency(tmp_path):
    relay = _ConcurrencyRelay(part_count=20)
    coordinator = DownloadCoordinator(
        relay,
        local_settings(
            tmp_path,
            download_max_concurrency=16,
            download_global_max_concurrency=32,
            download_max_temp_bytes=64,
        ),
    )
    deadline = asyncio.get_running_loop().time() + 5
    try:
        results = await asyncio.gather(
            coordinator._download_large(
                "file-a", relay.manifests["file-a"], deadline=deadline
            ),
            coordinator._download_large(
                "file-b", relay.manifests["file-b"], deadline=deadline
            ),
        )
        assert relay.max_active == 32
        assert relay.max_by_file == {"file-a": 16, "file-b": 16}
        assert all(result.size == 20 for result in results)
        for result in results:
            assert result.path is not None
            assert result.path.read_bytes() == b"x" * 20
            await result.cleanup()
        assert coordinator.disk_budget.used == 0
    finally:
        await coordinator.close()



def test_pwrite_all_retries_short_writes_until_buffer_is_complete(tmp_path, monkeypatch):
    path = tmp_path / "short-write.bin"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    real_pwrite = os.pwrite
    calls = []

    def short_pwrite(target_fd, data, offset):
        calls.append(len(data))
        return real_pwrite(target_fd, memoryview(data)[:max(1, min(3, len(data)))], offset)

    monkeypatch.setattr(download_module.os, "pwrite", short_pwrite)
    try:
        download_module._pwrite_all(fd, b"0123456789", 7)
        os.ftruncate(fd, 17)
    finally:
        os.close(fd)

    assert len(calls) > 1
    assert path.read_bytes() == b"\x00" * 7 + b"0123456789"


@pytest.mark.asyncio
async def test_part_write_zero_is_a_hard_failure_not_a_verified_success(tmp_path, monkeypatch):
    class OnePartRelay:
        @asynccontextmanager
        async def download_part_stream(self, task_id, number):
            yield _PayloadPartResponse(b"complete")

    coordinator = DownloadCoordinator(OnePartRelay(), local_settings(tmp_path))
    fd, raw_path = tempfile.mkstemp(dir=coordinator.root)
    path = Path(raw_path)
    monkeypatch.setattr(download_module.os, "pwrite", lambda *_args: 0)
    held_parts = {}
    try:
        with pytest.raises(OSError, match="no progress"):
            await coordinator._fetch_one_part(
                "task",
                {
                    "part_number": 1,
                    "size": 8,
                    "sha256": hashlib.sha256(b"complete").hexdigest(),
                },
                fd,
                8,
                asyncio.Semaphore(1),
                held_parts,
            )
        assert held_parts == {}
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)
        await coordinator.close()


class _PayloadPartResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload
        self.headers = {
            "content-length": str(len(payload)),
            "x-part-sha256": hashlib.sha256(payload).hexdigest(),
            "x-part-number": "1",
        }

    async def aiter_raw(self):
        yield self.payload


class _InterruptedPartResponse(_PayloadPartResponse):
    async def aiter_raw(self):
        yield self.payload[:3]
        raise DownloadPartTransportError("simulated TCP read interruption")


@pytest.mark.asyncio
async def test_interrupted_ready_part_read_retries_same_part_and_completes(tmp_path):
    payload = b"complete"

    class RetryOnceRelay:
        def __init__(self):
            self.attempts = 0

        @asynccontextmanager
        async def download_part_stream(self, task_id, number):
            self.attempts += 1
            if self.attempts == 1:
                yield _InterruptedPartResponse(payload)
            else:
                yield _PayloadPartResponse(payload)

    relay = RetryOnceRelay()
    coordinator = DownloadCoordinator(relay, local_settings(tmp_path))
    fd, raw_path = tempfile.mkstemp(dir=coordinator.root)
    path = Path(raw_path)
    held_parts = {}
    try:
        await coordinator._fetch_one_part(
            "task",
            {
                "part_number": 1,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            fd,
            len(payload),
            asyncio.Semaphore(1),
            held_parts,
        )
        os.fsync(fd)
        assert relay.attempts == 2
        assert held_parts == {1: len(payload)}
        assert os.pread(fd, len(payload), 0) == payload
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)
        await coordinator.close()

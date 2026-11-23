import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mitmproxy import http

from r2relay_local.addon import R2RelayAddon
from r2relay_local.config import Settings
from r2relay_local.predownload import PredownloadDownloader


URL = "https://acc.r2.cloudflarestorage.com/b/path/video%20one.bin?X-Amz-Signature=masked"
BODY = b"preplaced bytes"


class Backend:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def download_stream(self, url, headers):
        self.calls.append((url, headers))
        return "backend-result"

    async def close(self):
        self.closed = True


class HeaderOnlyStream(httpx.AsyncByteStream):
    def __init__(self):
        self.read_attempted = False
        self.closed = False

    async def __aiter__(self):
        self.read_attempted = True
        raise AssertionError("Predownload should not read R2 GET body")
        yield b"unused"

    async def aclose(self):
        self.closed = True


def settings(tmp_path, backend="local"):
    return Settings(
        max_part_bytes=128 * 1024 * 1024,
        global_budget_bytes=1024 * 1024 * 1024,
        source=tmp_path / "test.toml",
        download_enabled=True,
        download_backend=backend,
        download_predownload_enabled=True,
        download_predownload_dir=tmp_path / "cache" / "predownload",
    )


async def stream(result):
    return b"".join([part async for part in result.stream])


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_name", ["local", "remote"])
async def test_no_same_name_skips_probe_and_uses_selected_backend(tmp_path, backend_name):
    called = []
    backend = Backend()
    def handler(request):
        called.append(request)
        raise AssertionError("No GET should be issued on a filename miss")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wrapper = PredownloadDownloader(backend, settings(tmp_path, backend_name), client=client)
        try:
            root = settings(tmp_path).download_predownload_dir
            assert root.is_dir()
            assert await wrapper.download_stream(URL, [("X-Test", "raw")]) == "backend-result"
            assert len(backend.calls) == 1
            assert called == []
        finally:
            await wrapper.close()
        assert backend.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_name", ["local", "remote"])
async def test_same_name_matching_size_returns_local_body_and_r2_headers(tmp_path, backend_name):
    cfg = settings(tmp_path, backend_name)
    cfg.download_predownload_dir.mkdir(parents=True)
    (cfg.download_predownload_dir / "video one.bin").write_bytes(BODY)
    upstream = HeaderOnlyStream()
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, headers={"Content-Length": str(len(BODY)),
                          "ETag": '"actual-r2-version"', "Content-Type": "application/x-test"},
            stream=upstream,
        )
    backend = Backend()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wrapper = PredownloadDownloader(backend, cfg, client=client)
        try:
            original_headers = [("X-Test", "keep"), ("If-Match", '"actual-r2-version"')]
            result = await wrapper.download_stream(URL, original_headers)
            assert result.status_code == 200
            assert result.size == len(BODY)
            assert ("etag", '"actual-r2-version"') in [
                (k.lower(), v) for k, v in result.headers
            ]
            assert ("content-type", "application/x-test") in [
                (k.lower(), v) for k, v in result.headers
            ]
            assert await stream(result) == BODY
            await result.cleanup()
            await result.cleanup()
            assert backend.calls == []
            assert len(requests) == 1
            assert requests[0].headers["If-Match"] == '"actual-r2-version"'
            assert "Accept-Encoding" not in requests[0].headers
            assert upstream.closed and not upstream.read_attempted
            assert (cfg.download_predownload_dir / "video one.bin").read_bytes() == BODY
        finally:
            await wrapper.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_name", ["local", "remote"])
async def test_size_mismatch_uses_original_backend_with_original_request(tmp_path, backend_name):
    cfg = settings(tmp_path, backend_name)
    cfg.download_predownload_dir.mkdir(parents=True)
    (cfg.download_predownload_dir / "video one.bin").write_bytes(BODY)
    upstream = HeaderOnlyStream()
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(
            200, headers={"Content-Length": str(len(BODY) + 1)}, stream=upstream,
        )
    backend = Backend()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wrapper = PredownloadDownloader(backend, cfg, client=client)
        try:
            headers = [("Range-Fake", "keep")]
            assert await wrapper.download_stream(URL, headers) == "backend-result"
            assert backend.calls == [(URL, headers)]
            assert count == 1
            assert upstream.closed and not upstream.read_attempted
        finally:
            await wrapper.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("range_value, content_range, expected", [
    ("bytes=2-6", "bytes 2-6/15", BODY[2:7]),
    ("bytes=8-", "bytes 8-14/15", BODY[8:]),
])
async def test_206_reads_only_correct_local_range(tmp_path, range_value, content_range, expected):
    assert len(BODY) == 15
    cfg = settings(tmp_path)
    cfg.download_predownload_dir.mkdir(parents=True)
    (cfg.download_predownload_dir / "video one.bin").write_bytes(BODY)
    upstream = HeaderOnlyStream()
    def handler(request):
        assert request.headers["Range"] == range_value
        return httpx.Response(
            206, headers={
                "Content-Length": str(len(expected)), "Content-Range": content_range,
                "ETag": '"range-version"',
            }, stream=upstream,
        )
    backend = Backend()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wrapper = PredownloadDownloader(backend, cfg, client=client)
        try:
            result = await wrapper.download_stream(URL, [("Range", range_value)])
            assert result.status_code == 206
            assert result.size == len(expected)
            assert ("content-range", content_range) in [
                (k.lower(), v) for k, v in result.headers
            ]
            assert await stream(result) == expected
            assert backend.calls == []
            assert upstream.closed and not upstream.read_attempted
        finally:
            await wrapper.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status, headers, request_headers", [
    (404, {"Content-Length": str(len(BODY))}, []),
    (304, {"Content-Length": str(len(BODY))}, []),
    (200, {}, []),
    (200, {"Content-Length": str(len(BODY)), "Content-Encoding": "gzip"}, []),
    (206, {"Content-Length": "5", "Content-Range": "bytes 2-6/99"}, [("Range", "bytes=2-6")]),
    (206, {"Content-Length": "5", "Content-Range": "bytes 3-7/15"}, [("Range", "bytes=2-6")]),
    (200, {"Content-Length": str(len(BODY))}, [("Range", "bytes=0-5")]),
])
async def test_unsuitable_original_response_falls_back(
    tmp_path, status, headers, request_headers,
):
    cfg = settings(tmp_path)
    cfg.download_predownload_dir.mkdir(parents=True)
    (cfg.download_predownload_dir / "video one.bin").write_bytes(BODY)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, headers=headers, stream=HeaderOnlyStream()),
    )) as client:
        backend = Backend()
        wrapper = PredownloadDownloader(backend, cfg, client=client)
        try:
            assert await wrapper.download_stream(URL, request_headers) == "backend-result"
            assert backend.calls == [(URL, request_headers)]
        finally:
            await wrapper.close()


@pytest.mark.asyncio
async def test_symlink_and_unsafe_path_never_trigger_probe(tmp_path):
    cfg = settings(tmp_path)
    cfg.download_predownload_dir.mkdir(parents=True)
    (tmp_path / "elsewhere").write_bytes(BODY)
    (cfg.download_predownload_dir / "video one.bin").symlink_to(tmp_path / "elsewhere")
    called = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: called.append(request),
    )) as client:
        backend = Backend()
        wrapper = PredownloadDownloader(backend, cfg, client=client)
        try:
            assert await wrapper.download_stream(URL, []) == "backend-result"
            assert await wrapper.download_stream(
                "https://acc.r2.cloudflarestorage.com/b/a%2Fb", [],
            ) == "backend-result"
            assert called == []
            assert len(backend.calls) == 2
        finally:
            await wrapper.close()


@pytest.mark.asyncio
async def test_probe_error_falls_back_and_preserves_cancellation(tmp_path):
    cfg = settings(tmp_path)
    cfg.download_predownload_dir.mkdir(parents=True)
    (cfg.download_predownload_dir / "video one.bin").write_bytes(BODY)
    async def failed(request):
        raise httpx.ConnectError("offline")
    async with httpx.AsyncClient(transport=httpx.MockTransport(failed)) as client:
        backend = Backend()
        wrapper = PredownloadDownloader(backend, cfg, client=client)
        try:
            assert await wrapper.download_stream(URL, []) == "backend-result"
            assert len(backend.calls) == 1
        finally:
            await wrapper.close()


@pytest.mark.asyncio
async def test_addon_wraps_existing_backend_before_disk_response(tmp_path):
    cfg = settings(tmp_path, "remote")
    backend = Backend()
    addon = R2RelayAddon(
        relay=None, settings=cfg, download_coordinator=backend,
        upload_relay_enabled=False,
    )
    try:
        assert isinstance(addon.downloads, PredownloadDownloader)
        assert addon.downloads.backend is backend
        assert cfg.download_predownload_dir.is_dir()
    finally:
        await addon.done()
    assert backend.closed


@pytest.mark.asyncio
async def test_file_descriptor_pins_file_after_rename(tmp_path):
    cfg = settings(tmp_path)
    cfg.download_predownload_dir.mkdir(parents=True)
    candidate = cfg.download_predownload_dir / "video one.bin"
    candidate.write_bytes(BODY)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"Content-Length": str(len(BODY))},
            stream=HeaderOnlyStream(),
        ),
    )) as client:
        backend = Backend()
        wrapper = PredownloadDownloader(backend, cfg, client=client)
        try:
            result = await wrapper.download_stream(URL, [])
            candidate.rename(cfg.download_predownload_dir / "old.bin")
            candidate.write_bytes(b"modified content")
            assert await stream(result) == BODY
            assert backend.calls == []
        finally:
            await wrapper.close()

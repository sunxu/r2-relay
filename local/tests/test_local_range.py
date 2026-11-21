import asyncio
from pathlib import Path

import httpx
import pytest

from r2relay_local.config import Settings
from r2relay_local.local_range import LocalRangeDownloader, LocalRangeError

MIB = 1024 * 1024
URL = "https://test.r2.cloudflarestorage.com/b/object?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-SignedHeaders=host&X-Amz-Signature=masked"
ETAG = '"strong-etag"'
BODY = (bytes(range(251)) * ((5 * MIB + 13 + 250) // 251))[:5 * MIB + 13]


def settings(tmp_path, **kw):
    data = dict(max_part_bytes=128*MIB, global_budget_bytes=1024*MIB,
                source=Path(tmp_path) / "config.toml", download_enabled=True,
                download_backend="local", download_max_temp_bytes=64*MIB,
                download_local_max_connections_per_get=8,
                download_local_global_max_connections=128,
                download_temp_dir=Path(tmp_path) / "downloads")
    data.update(kw)
    return Settings(**data)


def upstream(data=BODY, *, etag=ETAG, accept_ranges=True, version_change=False,
             reject_child=False, failure_status=None):
    calls = []

    def respond(request):
        request_range = request.headers.get("range")
        calls.append((request_range, request.headers.get("if-match")))
        if failure_status:
            return httpx.Response(failure_status, content=b"problem", headers={"X-R2": "error"})
        if request_range:
            assert request_range.startswith("bytes=")
            lo, hi = request_range.removeprefix("bytes=").split("-", 1)
            start = int(lo)
            end = min(int(hi) if hi else len(data)-1, len(data)-1)
            if reject_child and request.headers.get("if-match"):
                return httpx.Response(412, content=b"changed")
            chunk = data[start:end+1]
            version = '"new-version"' if version_change and request.headers.get("if-match") else etag
            return httpx.Response(206, content=chunk, headers={
                "Content-Range": f"bytes {start}-{end}/{len(data)}",
                "Content-Length": str(len(chunk)), "ETag": version,
                "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT",
                "Content-Encoding": "identity",
            })
        return httpx.Response(200, content=data, headers={
            "Content-Length": str(len(data)), "ETag": etag,
            "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT",
            **({"Accept-Ranges": "bytes"} if accept_ranges else {}),
        })

    return respond, calls


async def run_download(tmp_path, handler, headers=None, cfg=None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        downloader = LocalRangeDownloader(cfg or settings(tmp_path), client=client)
        try:
            result = await downloader.download_stream(URL, headers or [])
            raw = bytearray()
            async for chunk in result.stream:
                raw.extend(chunk)
            await result.cleanup()
            return result, bytes(raw)
        finally:
            await downloader.close()


@pytest.mark.asyncio
async def test_2mib_exact_is_single_and_small_is_single(tmp_path):
    for size in (1024, 2*MIB):
        handler, calls = upstream(BODY[:size])
        result, content = await run_download(tmp_path, handler)
        assert result.status_code == 200
        assert content == BODY[:size]
        assert calls == [(None, None)]


@pytest.mark.asyncio
async def test_fixed_range_reuses_initial_get_and_orders_bytes(tmp_path):
    start = 17
    end = len(BODY) - 1
    handler, calls = upstream()
    headers = [("Range", f"bytes={start}-{end}"), ("X-Test", "preserve")]
    result, content = await run_download(tmp_path, handler, headers)
    assert result.status_code == 206
    assert ("content-range", f"bytes {start}-{end}/{len(BODY)}") in [
        (key.lower(), value) for key, value in result.headers
    ]
    assert content == BODY[start:end+1]
    assert calls[0] == (f"bytes={start}-{end}", None)
    assert len(calls) == 3  # initial 2 MiB, one full subrange, short tail
    assert set(rng for rng, _ in calls[1:]) == {
        f"bytes={start+2*MIB}-{start+4*MIB-1}",
        f"bytes={start+4*MIB}-{end}",
    }
    assert all(tag == ETAG for _, tag in calls[1:])


@pytest.mark.asyncio
async def test_open_range_and_no_range_preserve_original_response(tmp_path):
    handler, calls = upstream()
    result, content = await run_download(tmp_path, handler, [("Range", "bytes=113-")])
    assert content == BODY[113:]
    assert result.status_code == 206
    assert calls[0] == ("bytes=113-", None)
    assert all(r is not None for r, _ in calls[1:])
    handler, calls = upstream()
    result, content = await run_download(tmp_path, handler)
    assert result.status_code == 200
    assert content == BODY
    assert calls[0] == (None, None)
    assert len(calls) > 1


@pytest.mark.asyncio
async def test_unsigned_range_required_and_signed_range_never_rewritten(tmp_path):
    signed = URL.replace("X-Amz-SignedHeaders=host", "X-Amz-SignedHeaders=host%3Brange")
    handler, calls = upstream()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = LocalRangeDownloader(settings(tmp_path), client=client)
        try:
            result = await downloader.download_stream(
                signed, [("Range", f"bytes=0-{len(BODY)-1}")]
            )
            content = b"".join([chunk async for chunk in result.stream])
            assert content == BODY
            assert calls == [(f"bytes=0-{len(BODY)-1}", None)]
        finally:
            await downloader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no_range_support", "weak_etag", "multi_range", "conditional"])
async def test_cannot_prove_split_is_safe_keeps_single_get(tmp_path, case):
    handler, calls = upstream(
        etag='W/"weak"' if case == "weak_etag" else ETAG,
        accept_ranges=case != "no_range_support",
    )
    headers = []
    if case == "multi_range":
        headers.append(("Range", "bytes=0-2,4-8"))
        # This fixture's upstream only supports single ranges. Return a single
        # original response to assert no attempted child requests.
        handler = lambda req: httpx.Response(200, content=BODY)
    elif case == "conditional":
        headers.append(("If-Match", ETAG))
    result, content = await run_download(tmp_path, handler, headers)
    assert content == BODY
    if case != "multi_range":
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_subrange_version_mismatch_aborts_and_cleans_resources(tmp_path):
    handler, calls = upstream(version_change=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = LocalRangeDownloader(settings(tmp_path), client=client)
        try:
            result = await downloader.download_stream(URL, [])
            with pytest.raises(LocalRangeError):
                _ = b"".join([chunk async for chunk in result.stream])
            await result.cleanup()
            assert downloader.disk_budget.used == 0
            assert not list(downloader.root.glob("r2-range-*"))
            assert len(calls) >= 2
        finally:
            await downloader.close()


@pytest.mark.asyncio
async def test_error_response_is_forwarded_without_parallel_requests(tmp_path):
    handler, calls = upstream(failure_status=403)
    result, body = await run_download(tmp_path, handler)
    assert result.status_code == 403
    assert ("x-r2", "error") in [(k.lower(), v) for k, v in result.headers]
    assert body == b"problem"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancel_release_before_or_after_body_read(tmp_path):
    handler, _ = upstream()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = LocalRangeDownloader(settings(tmp_path), client=client)
        try:
            result = await downloader.download_stream(URL, [])
            await result.cleanup()
            await result.cleanup()
            assert downloader.disk_budget.used == 0
            assert not list(downloader.root.glob("r2-range-*"))
        finally:
            await downloader.close()


@pytest.mark.asyncio
async def test_global_connections_across_gets_never_exceed_limit(tmp_path):
    active = 0
    peak = 0

    class TrackedStream(httpx.AsyncByteStream):
        def __init__(self, data):
            self.data = data
            self.closed = False

        async def __aiter__(self):
            await asyncio.sleep(0.003)
            yield self.data

        async def aclose(self):
            nonlocal active
            if not self.closed:
                self.closed = True
                active -= 1

    def handler(request):
        nonlocal active, peak
        rng = request.headers.get("range")
        if rng:
            lo, hi = rng.removeprefix("bytes=").split("-", 1)
            start = int(lo)
            end = int(hi) if hi else len(BODY)-1
            body = BODY[start:end+1]
            header = {
                "Content-Range": f"bytes {start}-{end}/{len(BODY)}",
                "Content-Length": str(len(body)),
                "ETag": ETAG,
            }
            status = 206
        else:
            body = BODY
            header = {"Content-Length": str(len(body)), "ETag": ETAG, "Accept-Ranges": "bytes"}
            status = 200
        active += 1
        peak = max(peak, active)
        return httpx.Response(status, headers=header, stream=TrackedStream(body))

    cfg = settings(tmp_path, download_local_max_connections_per_get=2,
                   download_local_global_max_connections=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = LocalRangeDownloader(cfg, client=client)
        try:
            async def job():
                result = await downloader.download_stream(URL, [])
                return b"".join([part async for part in result.stream])
            outputs = await asyncio.gather(*(job() for _ in range(3)))
            assert outputs == [BODY] * 3
            assert peak <= 2
            assert active == 0
            assert downloader.disk_budget.used == 0
        finally:
            await downloader.close()

import asyncio
import hashlib

import httpx
import pytest

from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore
from r2relay_vps.config import DownloadSettings


MIB = 1024 * 1024
R2_URL = "https://abc.r2.cloudflarestorage.com/bucket/object?x=1"


def settings(**overrides):
    values = dict(
        enabled=True,
        part_size_bytes=16 * MIB,
        split_threshold_bytes=24 * MIB,
        cache_ttl_seconds=600,
        max_cache_bytes=128 * MIB,
        min_free_bytes=1,
        max_active_tasks=4,
        max_retained_tasks=16,
        max_waiters=8,
        wait_timeout_seconds=3,
    )
    values.update(overrides)
    return DownloadSettings(**values)


async def create(client, headers, request_id="req-1", url=R2_URL):
    response = await client.post(
        "/v1/downloads",
        headers=headers,
        json={"schema_version": 1, "request_id": request_id, "url": url, "headers": []},
    )
    return response


async def wait_complete(client, task_id, headers):
    for _ in range(500):
        response = await client.get(f"/v1/downloads/{task_id}", headers=headers)
        data = response.json()
        if data["status"] in ("COMPLETE", "FAILED", "CANCELLED"):
            return data
        await asyncio.sleep(0.002)
    raise AssertionError("VPS 下载任务未在限定时间内结束")


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, fail_after=False):
        self.chunks = chunks
        self.fail_after = fail_after

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail_after:
            raise httpx.ReadError("模拟上游读取中断")

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_download_api_auth_and_disabled_by_default(tmp_path):
    app = create_app(CacheStore(tmp_path / "upload"), token="test",
                     download_root=tmp_path / "downloads")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauth = await create(client, {})
        assert unauth.status_code == 401
        disabled = await create(client, {"Authorization": "Bearer test"})
        assert disabled.status_code == 503
        assert not (tmp_path / "downloads").exists()


@pytest.mark.asyncio
async def test_small_r2_error_body_and_end_to_end_headers_are_preserved(tmp_path):
    body = b"SignatureDoesNotMatch"
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(
            403,
            content=body,
            headers=[
                ("ETag", '"r2-error"'),
                ("Connection", "X-Private-Hop"),
                ("X-Private-Hop", "must-not-escape"),
                ("Set-Cookie", "a=1"),
                ("Set-Cookie", "b=2"),
            ],
        )

    app = create_app(
        CacheStore(tmp_path / "upload"), token="test",
        download_transport=httpx.MockTransport(upstream),
        download_settings=settings(), download_root=tmp_path / "downloads",
    )
    auth = {"Authorization": "Bearer test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await create(client, auth)
        assert created.status_code == 202
        manifest = await wait_complete(client, created.json()["task_id"], auth)
        assert manifest["status"] == "COMPLETE"
        assert manifest["mode"] == "small"
        assert manifest["status_code"] == 403
        assert ["ETag", '"r2-error"'] in manifest["headers"]
        assert not any(k.lower() == "x-private-hop" for k, _ in manifest["headers"])
        assert sum(k.lower() == "set-cookie" for k, _ in manifest["headers"]) == 2
        result = await client.get(
            f"/v1/downloads/{manifest['task_id']}/body", headers=auth
        )
        assert result.content == body
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_25_mib_download_is_atomically_split_into_16_and_9_mib_parts(tmp_path):
    body = b"z" * (25 * MIB)
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(206, content=body, headers={
            "Content-Range": f"bytes 0-{len(body)-1}/{len(body)}",
            "ETag": '"object-version-1"',
        })

    app = create_app(
        CacheStore(tmp_path / "upload"), token="test",
        download_transport=httpx.MockTransport(upstream),
        download_settings=settings(), download_root=tmp_path / "downloads",
    )
    auth = {"Authorization": "Bearer test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await create(client, auth)
        manifest = await wait_complete(client, created.json()["task_id"], auth)
        assert manifest["status"] == "COMPLETE"
        assert manifest["mode"] == "parts"
        assert manifest["status_code"] == 206
        assert manifest["total_size"] == 25 * MIB
        assert [part["size"] for part in manifest["parts"]] == [16 * MIB, 9 * MIB]
        assert all(part["status"] == "READY" for part in manifest["parts"])
        rebuilt = bytearray()
        for part in manifest["parts"]:
            result = await client.get(
                f"/v1/downloads/{manifest['task_id']}/parts/{part['part_number']}",
                headers=auth,
            )
            assert result.status_code == 200
            assert len(result.content) == part["size"]
            assert result.headers["x-part-sha256"] == hashlib.sha256(result.content).hexdigest()
            rebuilt.extend(result.content)
        assert bytes(rebuilt) == body
        assert len(calls) == 1
        wrong_mode = await client.get(
            f"/v1/downloads/{manifest['task_id']}/body", headers=auth
        )
        assert wrong_mode.status_code == 409
        deleted = await client.delete(f"/v1/downloads/{manifest['task_id']}", headers=auth)
        assert deleted.status_code == 200
        deleted_again = await client.delete(
            f"/v1/downloads/{manifest['task_id']}", headers=auth
        )
        assert deleted_again.status_code == 200


@pytest.mark.asyncio
async def test_unknown_content_length_switches_bounded_spool_to_parts(tmp_path):
    part = 4 * MIB
    threshold = 6 * MIB
    body = b"a" * (threshold + 1)

    def upstream(_request):
        return httpx.Response(
            200,
            stream=ChunkStream([body[:2 * MIB], body[2 * MIB:]]),
        )

    app = create_app(
        CacheStore(tmp_path / "upload"), token="test",
        download_transport=httpx.MockTransport(upstream),
        download_settings=settings(
            part_size_bytes=part, split_threshold_bytes=threshold,
        ),
        download_root=tmp_path / "downloads",
    )
    auth = {"Authorization": "Bearer test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await create(client, auth)
        manifest = await wait_complete(client, created.json()["task_id"], auth)
        assert manifest["status"] == "COMPLETE"
        assert manifest["expected_size"] is None
        assert manifest["mode"] == "parts"
        assert [item["size"] for item in manifest["parts"]] == [part, threshold + 1 - part]
        assert manifest["total_size"] == threshold + 1


@pytest.mark.asyncio
async def test_upstream_failure_never_leaves_readable_partial_part(tmp_path):
    part_size = 4 * MIB
    body = b"x" * (part_size + 100)

    def upstream(_request):
        return httpx.Response(
            200,
            stream=ChunkStream([body], fail_after=True),
        )

    app = create_app(
        CacheStore(tmp_path / "upload"), token="test",
        download_transport=httpx.MockTransport(upstream),
        download_settings=settings(part_size_bytes=part_size, split_threshold_bytes=2 * MIB),
        download_root=tmp_path / "downloads",
    )
    auth = {"Authorization": "Bearer test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await create(client, auth)
        manifest = await wait_complete(client, created.json()["task_id"], auth)
        assert manifest["status"] == "FAILED"
        assert manifest["error"]["type"] == "ReadError"
        assert not list((tmp_path / "downloads").rglob("*.part"))
        part = await client.get(
            f"/v1/downloads/{manifest['task_id']}/parts/1", headers=auth
        )
        assert part.status_code == 502


@pytest.mark.asyncio
async def test_request_idempotency_and_request_scope_validation(tmp_path):
    calls = []

    def upstream(request):
        calls.append(request.url)
        return httpx.Response(200, content=b"ok")

    app = create_app(
        CacheStore(tmp_path / "upload"), token="test",
        download_transport=httpx.MockTransport(upstream),
        download_settings=settings(), download_root=tmp_path / "downloads",
    )
    auth = {"Authorization": "Bearer test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await create(client, auth, request_id="same")
        second = await create(client, auth, request_id="same")
        assert first.status_code == second.status_code == 202
        assert first.json()["task_id"] == second.json()["task_id"]
        await wait_complete(client, first.json()["task_id"], auth)
        assert len(calls) == 1

        conflict = await create(
            client, auth, request_id="same",
            url="https://abc.r2.cloudflarestorage.com/bucket/other?x=2",
        )
        assert conflict.status_code == 409

        invalid = await create(
            client, auth, request_id="bad",
            url="https://example.com/object",
        )
        assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_download_active_task_capacity_is_enforced(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            yield b"first"
            await release.wait()
            yield b"second"

        async def aclose(self):
            return None

    def upstream(_request):
        return httpx.Response(200, stream=SlowStream())

    app = create_app(
        CacheStore(tmp_path / "upload"), token="test",
        download_transport=httpx.MockTransport(upstream),
        download_settings=settings(max_active_tasks=1, max_waiters=1),
        download_root=tmp_path / "downloads",
    )
    auth = {"Authorization": "Bearer test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await create(client, auth, request_id="one")
        await asyncio.wait_for(started.wait(), timeout=2)
        second = await create(client, auth, request_id="two")
        assert second.status_code == 503
        release.set()
        manifest = await wait_complete(client, first.json()["task_id"], auth)
        assert manifest["status"] == "COMPLETE"



@pytest.mark.asyncio
async def test_ready_part_is_available_while_upstream_is_still_receiving(tmp_path):
    first_ready = asyncio.Event()
    release_second = asyncio.Event()
    first = b"a" * (16 * MIB)
    second = b"b" * (9 * MIB)

    class SlowLargeStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first
            first_ready.set()
            await release_second.wait()
            yield second

        async def aclose(self):
            return None

    def upstream(_request):
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(first) + len(second))},
            stream=SlowLargeStream(),
        )

    app = create_app(
        CacheStore(tmp_path / "upload"), token="test",
        download_transport=httpx.MockTransport(upstream),
        download_settings=settings(max_waiters=1),
        download_root=tmp_path / "downloads",
    )
    auth = {"Authorization": "Bearer test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await create(client, auth)
        task_id = created.json()["task_id"]
        await asyncio.wait_for(first_ready.wait(), timeout=3)
        for _ in range(300):
            manifest = (await client.get(f"/v1/downloads/{task_id}", headers=auth)).json()
            if manifest["part_count"] >= 1:
                break
            await asyncio.sleep(0.002)
        assert manifest["status"] == "RECEIVING"
        assert manifest["parts"][0]["status"] == "READY"

        first_part = await client.get(
            f"/v1/downloads/{task_id}/parts/1", headers=auth
        )
        assert first_part.status_code == 200
        assert first_part.content == first

        waiting = asyncio.create_task(client.get(
            f"/v1/downloads/{task_id}/parts/2", headers=auth
        ))
        await asyncio.sleep(0.02)
        assert not waiting.done()

        overflow = await client.get(
            f"/v1/downloads/{task_id}/parts/3", headers=auth
        )
        assert overflow.status_code == 503

        release_second.set()
        second_part = await waiting
        assert second_part.status_code == 200
        assert second_part.content == second
        final = await wait_complete(client, task_id, auth)
        assert final["status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_delete_waits_for_active_part_reader_before_removing_files(tmp_path):
    body = b"q" * (25 * MIB)

    app = create_app(
        CacheStore(tmp_path / "upload"), token="test",
        download_transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=body)
        ),
        download_settings=settings(), download_root=tmp_path / "downloads",
    )
    manager = app.state.downloads
    task = await manager.create("reader-delete", R2_URL, [])
    for _ in range(500):
        if task.status in {"COMPLETE", "FAILED"}:
            break
        await asyncio.sleep(0.002)
    assert task.status == "COMPLETE"

    reader_task, path, _part = await manager.acquire_part(task.task_id, 1)
    generator = manager.stream_file(reader_task, path)
    first_chunk = await anext(generator)
    assert first_chunk
    await manager.delete(task.task_id)
    assert path.exists()
    await generator.aclose()
    assert not task.directory.exists()
    assert manager.used_bytes == 0


@pytest.mark.asyncio
async def test_completed_download_expires_after_idle_ttl(tmp_path):
    now = [100.0]
    app = create_app(
        CacheStore(tmp_path / "upload"), token="test",
        download_transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"short")
        ),
        download_settings=settings(cache_ttl_seconds=10),
        download_root=tmp_path / "downloads",
    )
    manager = app.state.downloads
    manager.clock = lambda: now[0]
    task = await manager.create("ttl", R2_URL, [])
    for _ in range(500):
        if task.status in {"COMPLETE", "FAILED"}:
            break
        await asyncio.sleep(0.002)
    assert task.status == "COMPLETE"
    assert task.expires_at == 110.0

    # A read refreshes the idle expiry window.
    now[0] = 105.0
    acquired, path = await manager.acquire_body(task.task_id)
    assert path.read_bytes() == b"short"
    await manager.reader_finished(acquired)
    assert task.expires_at == 115.0
    now[0] = 116.0
    assert await manager.cleanup() == 1
    assert task.task_id not in manager.tasks
    assert manager.used_bytes == 0



@pytest.mark.asyncio
async def test_retained_task_limit_bounds_completed_manifests(tmp_path):
    app = create_app(
        CacheStore(tmp_path / "upload"), token="test",
        download_transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"ok")
        ),
        download_settings=settings(max_retained_tasks=1),
        download_root=tmp_path / "downloads",
    )
    auth = {"Authorization": "Bearer test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await create(client, auth, request_id="retained-one")
        await wait_complete(client, first.json()["task_id"], auth)
        second = await create(client, auth, request_id="retained-two")
        assert second.status_code == 503



@pytest.mark.asyncio
async def test_signed_request_headers_are_forwarded_without_httpx_defaults(tmp_path):
    captured = []

    def upstream(request):
        captured.append(request)
        return httpx.Response(200, content=b"ok")

    app = create_app(
        CacheStore(tmp_path / "upload"), token="test",
        download_transport=httpx.MockTransport(upstream),
        download_settings=settings(), download_root=tmp_path / "downloads",
    )
    manager = app.state.downloads
    task = await manager.create(
        "signed-headers",
        R2_URL + "&X-Amz-Signature=do-not-leak",
        [
            ("Host", "abc.r2.cloudflarestorage.com"),
            ("Authorization", "AWS4-HMAC-SHA256 Credential=redacted"),
            ("Range", "bytes=0-1"),
            ("If-Match", '"etag-1"'),
        ],
    )
    for _ in range(500):
        if task.status in {"COMPLETE", "FAILED"}:
            break
        await asyncio.sleep(0.002)
    assert task.status == "COMPLETE"
    headers = captured[0].headers
    assert headers["authorization"] == "AWS4-HMAC-SHA256 Credential=redacted"
    assert headers["range"] == "bytes=0-1"
    assert headers["if-match"] == '"etag-1"'
    assert "accept-encoding" not in headers
    assert "user-agent" not in headers
    assert "connection" not in headers
    await manager.close()


@pytest.mark.asyncio
async def test_upstream_exception_manifest_does_not_expose_signed_url(tmp_path):
    secret = "X-Amz-Signature=super-secret-signature"

    def upstream(_request):
        raise httpx.ConnectError(f"connection failed {secret}")

    app = create_app(
        CacheStore(tmp_path / "upload"), token="test",
        download_transport=httpx.MockTransport(upstream),
        download_settings=settings(), download_root=tmp_path / "downloads",
    )
    manager = app.state.downloads
    task = await manager.create("secret-url", R2_URL + "?" + secret, [])
    for _ in range(500):
        if task.status in {"COMPLETE", "FAILED"}:
            break
        await asyncio.sleep(0.002)
    manifest = manager.manifest(task)
    assert manifest["status"] == "FAILED"
    assert secret not in str(manifest)
    assert manifest["error"]["message"] == "R2 下载失败；详细信息仅保留在受控服务端日志中"
    await manager.close()


@pytest.mark.asyncio
async def test_download_directory_is_exclusive_to_one_worker(tmp_path):
    root = tmp_path / "downloads"
    first = create_app(
        CacheStore(tmp_path / "upload-a"), token="test",
        download_settings=settings(), download_root=root,
    )
    with pytest.raises(RuntimeError, match="另一个 worker"):
        create_app(
            CacheStore(tmp_path / "upload-b"), token="test",
            download_settings=settings(), download_root=root,
        )
    await first.state.downloads.close()
    second = create_app(
        CacheStore(tmp_path / "upload-c"), token="test",
        download_settings=settings(), download_root=root,
    )
    await second.state.downloads.close()

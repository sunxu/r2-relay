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
            url="https://evil.example.com/object",
        )
        assert conflict.status_code == 409

        invalid = await create(
            client, auth, request_id="bad",
            url="https://example.com/object",
        )
        assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_download_capacity_and_part_waiter_limit(tmp_path):
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

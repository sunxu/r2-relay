"""Authenticated VPS cache negotiation and R2 execution API."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .cache import CacheStore, MD5_PATTERN
from .config import DownloadSettings, load_download_settings
from .downloads import DownloadError, DownloadManager
from .worker import R2Worker

LOG = logging.getLogger('r2relay.vps')


class R2Request(BaseModel):
    method: str
    url: str
    headers: list[tuple[str, str]] = Field(default_factory=list)


class Prepare(BaseModel):
    schema_version: int = 1
    request_id: str
    md5: str
    size: int = Field(ge=0)
    r2_request: R2Request


class DownloadCreate(BaseModel):
    schema_version: int = 1
    request_id: str
    url: str
    headers: list[tuple[str, str]] = Field(default_factory=list)


class State:
    def __init__(self, store: CacheStore, token: str, worker: R2Worker):
        self.store = store
        self.token = token
        self.worker = worker
        self.transfers: dict[str, tuple[str, Prepare, float]] = {}
        self.executions: dict[str, dict] = {}
        self.tasks: set[asyncio.Task] = set()
        self.metrics = {
            "prepare_hit": 0, "prepare_miss": 0, "staged_bytes": 0,
            "r2_executions_finished": 0, "r2_attempts": 0,
            "r2_success": 0, "r2_failed": 0, "cache_unavailable": 0,
        }
        self.max_executions = int(os.environ.get("R2_RELAY_MAX_EXECUTIONS", "512"))
        self.max_concurrent = int(os.environ.get("R2_RELAY_MAX_CONCURRENT", "16"))
        self.execution_ttl = int(os.environ.get("R2_RELAY_EXECUTION_TTL", "900"))
        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        # Cap retained base64 response payloads independently of execution count.
        self.max_retained_result_bytes = int(
            os.environ.get("R2_RELAY_MAX_RETAINED_RESULT_BYTES", str(64 * 1024 * 1024))
        )
        self.retained_result_bytes = 0
        self.retained_sizes: dict[int, int] = {}

    def prune(self):
        now = time.time()
        expired = [key for key, record in self.executions.items()
                   if record["status"] in ("FAILED", "SUCCEEDED")
                   and now - record.get("finished_at", now) >= self.execution_ttl]
        for key in expired:
            item = self.executions.pop(key, None)
            if item is not None:
                self.retained_result_bytes -= self.retained_sizes.pop(id(item), 0)
        stale = [key for key, (_execution_id, _p, created) in self.transfers.items()
                 if now - created >= self.execution_ttl]
        for key in stale:
            execution_id, _p, _created = self.transfers.pop(key)
            record = self.executions.get(execution_id)
            if record and record["status"] == "QUEUED":
                record["status"] = "FAILED"
                record["finished_at"] = now
                record["relay_error"] = {"type": "TransferExpired", "message": "no body uploaded before TTL"}

    async def run(self, record: dict, path: Path):
        async with self.semaphore:
            try:
                await self.worker.execute(record, path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                record["status"] = "FAILED"
                record["relay_error"] = {"type": type(exc).__name__, "message": str(exc)}
            finally:
                # A full response body can be up to 8 MiB. Without a separate
                # retention budget, 512 terminal records could exhaust VPS RAM.
                upstream = record.get("upstream_response")
                retained = len(upstream.get("body_base64", "")) if upstream else 0
                if retained and self.retained_result_bytes + retained > self.max_retained_result_bytes:
                    record.pop("upstream_response", None)
                    record["status"] = "FAILED"
                    record["relay_error"] = {
                        "type": "ResultMemoryBudgetExceeded",
                        "message": "R2 result cannot be safely retained",
                    }
                else:
                    self.retained_result_bytes += retained
                    self.retained_sizes[id(record)] = retained
                record["finished_at"] = time.time()
                self.metrics["r2_executions_finished"] += 1
                self.metrics["r2_attempts"] += record.get("attempts", 0)
                if record["status"] == "SUCCEEDED":
                    self.metrics["r2_success"] += 1
                else:
                    self.metrics["r2_failed"] += 1

    def start(self, execution_id: str):
        record = self.executions[execution_id]
        entry = self.store.get(record["request"]["md5"])
        if entry is None:
            record["status"] = "FAILED"
            record["relay_error"] = {"type": "CacheUnavailable", "message": "part expired or missing"}
            record["finished_at"] = time.time()
            return
        task = asyncio.create_task(self.run(record, entry.path))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


def create_app(store: CacheStore | None = None, token: str | None = None,
               r2_transport: httpx.AsyncBaseTransport | None = None,
               retry_delays: tuple[float, ...] = (2, 4, 8),
               download_transport: httpx.AsyncBaseTransport | None = None,
               download_settings: DownloadSettings | None = None,
               download_root: Path | None = None,
               download_config_path: Path | None = None,
               download_override_path: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_cleanup_loop(app.state.relay))
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            active = list(app.state.relay.tasks)
            for running in active:
                running.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            await app.state.downloads.close()

    app = FastAPI(title="R2 Relay VPS", lifespan=lifespan)
    cache_store = store or CacheStore(Path(os.environ.get("R2_RELAY_CACHE_DIR", "/var/lib/r2-relay/cache")))
    state = State(cache_store,
                  token if token is not None else os.environ.get("R2_RELAY_TOKEN", ""),
                  R2Worker(transport=r2_transport, delays=retry_delays))
    if not state.token:
        raise RuntimeError("R2_RELAY_TOKEN must be configured")
    settings = download_settings or load_download_settings(
        download_config_path, download_override_path
    )
    downloads = DownloadManager(
        download_root or Path("/var/lib/r2-relay/downloads"), settings,
        transport=download_transport,
    )
    state.downloads = downloads
    app.state.downloads = downloads
    app.state.relay = state

    def authorize(authorization: str | None):
        if not authorization or not secrets.compare_digest(authorization, f"Bearer {state.token}"):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "download_enabled": settings.enabled}

    # Dedicated network benchmark sink: authenticated, bounded, streamed and discarded.
    bench_max_bytes = int(os.environ.get("R2_RELAY_BENCH_MAX_BYTES", str(32 * 1024 * 1024)))
    bench_semaphore = asyncio.Semaphore(
        int(os.environ.get("R2_RELAY_BENCH_MAX_CONCURRENT", "4"))
    )

    @app.put("/v1/bench/sink")
    async def benchmark_sink(request: Request,
                             authorization: str | None = Header(default=None)):
        authorize(authorization)
        raw_length = request.headers.get("content-length")
        try:
            expected = int(raw_length) if raw_length is not None else -1
        except ValueError:
            expected = -1
        if expected < 0 or expected > bench_max_bytes:
            raise HTTPException(status_code=413, detail="invalid or excessive benchmark body size")

        async with bench_semaphore:
            started = time.perf_counter()
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > expected or received > bench_max_bytes:
                    raise HTTPException(status_code=413, detail="benchmark body exceeds declared size")
            elapsed = time.perf_counter() - started
            if received != expected:
                raise HTTPException(status_code=400, detail="benchmark body length mismatch")
        mbps = (received * 8 / elapsed / 1_000_000) if elapsed > 0 else 0.0
        return {"status": "received_and_discarded", "received_bytes": received,
                "server_elapsed_ms": round(elapsed * 1000, 2),
                "server_mbps": round(mbps, 2)}

    @app.post("/v1/parts/prepare")
    async def prepare(p: Prepare, authorization: str | None = Header(default=None)):
        authorize(authorization)
        if p.schema_version != 1 or not MD5_PATTERN.fullmatch(p.md5):
            raise HTTPException(status_code=400, detail="invalid schema or md5")
        max_part_bytes = int(os.environ.get("R2_RELAY_MAX_PART_BYTES", str(32 * 1024 * 1024)))
        if p.size > max_part_bytes:
            raise HTTPException(status_code=413, detail="part exceeds configured maximum")
        try:
            u = urlsplit(p.r2_request.url)
            query = parse_qs(u.query)
            valid_port = u.port in (None, 443)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid R2 UploadPart URL") from exc
        if (p.r2_request.method != "PUT" or u.scheme != "https" or not valid_port
                or not u.hostname or not u.hostname.endswith(".r2.cloudflarestorage.com")
                or u.hostname == ".r2.cloudflarestorage.com"
                or "uploadId" not in query or "partNumber" not in query):
            raise HTTPException(status_code=400, detail="invalid R2 UploadPart target")
        signed_host = [v for k, v in p.r2_request.headers if k.lower() == "host"]
        if signed_host and any(v.lower() != u.hostname.lower() for v in signed_host):
            raise HTTPException(status_code=400, detail="signed host mismatch")
        state.prune()
        if len(state.executions) >= state.max_executions or len(state.transfers) >= state.max_executions:
            raise HTTPException(status_code=503, detail="VPS execution capacity reached")
        execution_id = secrets.token_urlsafe(20)
        state.executions[execution_id] = {
            "status": "QUEUED", "attempts": 0, "request": p.model_dump(), "created_at": time.time()
        }
        if state.store.get(p.md5) is not None:
            state.metrics["prepare_hit"] += 1
            LOG.info("prepare HIT md5=%s size=%s execution=%s url=%s",
                     p.md5, p.size, execution_id, p.r2_request.url)
            state.start(execution_id)
            return {"status": "HIT", "execution_id": execution_id}
        state.metrics["prepare_miss"] += 1
        LOG.info("prepare MISS md5=%s size=%s execution=%s url=%s",
                 p.md5, p.size, execution_id, p.r2_request.url)
        transfer_id = secrets.token_urlsafe(20)
        state.transfers[transfer_id] = (execution_id, p, time.time())
        return {"status": "MISS", "transfer_id": transfer_id, "execution_id": execution_id}

    @app.put("/v1/parts/data/{transfer_id}")
    async def upload_data(transfer_id: str, request: Request,
                          authorization: str | None = Header(default=None),
                          x_content_md5_hex: str | None = Header(default=None)):
        authorize(authorization)
        item = state.transfers.get(transfer_id)
        if item is None:
            raise HTTPException(status_code=404, detail="unknown transfer")
        execution_id, p, _created = item
        if x_content_md5_hex != p.md5:
            raise HTTPException(status_code=400, detail="md5 metadata mismatch")
        try:
            entry = await state.store.put(p.md5, request.stream(), p.size)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            state.metrics["cache_unavailable"] += 1
            LOG.warning("cache failure md5=%s execution=%s error=%s",
                        p.md5, execution_id, exc)
            # No R2 worker has been started. HTTP 507 explicitly authorizes
            # local direct fallback without a competing upload.
            record = state.executions.get(execution_id)
            if record is not None:
                record["status"] = "FAILED"
                record["finished_at"] = time.time()
                record["relay_error"] = {
                    "type": "CacheStorageUnavailable", "message": str(exc)
                }
            raise HTTPException(status_code=507, detail="VPS cache unavailable") from exc
        finally:
            state.transfers.pop(transfer_id, None)
        state.metrics["staged_bytes"] += entry.size
        LOG.info("staged md5=%s size=%s execution=%s",
                 p.md5, entry.size, execution_id)
        state.start(execution_id)
        return {"status": "STAGED", "md5": entry.md5, "size": entry.size,
                "execution_id": execution_id}

    @app.post("/v1/downloads", status_code=202)
    async def create_download(p: DownloadCreate, authorization: str | None = Header(default=None)):
        authorize(authorization)
        if p.schema_version != 1:
            raise HTTPException(status_code=400, detail="unsupported download schema_version")
        try:
            task = await downloads.create(p.request_id, p.url, p.headers)
        except DownloadError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"task_id": task.task_id, "status": task.status, "request_id": task.request_id}

    @app.get("/v1/downloads/{task_id}")
    async def download_manifest(task_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        try:
            return downloads.manifest(downloads.get(task_id))
        except DownloadError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/v1/downloads/{task_id}/parts/{part_number}")
    async def download_part(task_id: str, part_number: int,
                            authorization: str | None = Header(default=None)):
        authorize(authorization)
        try:
            task, path, part = await downloads.acquire_part(task_id, part_number)
        except DownloadError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return StreamingResponse(
            downloads.stream_file(task, path),
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(part["size"]),
                "X-Part-SHA256": part["sha256"],
                "X-Part-Number": str(part["part_number"]),
            },
        )

    @app.get("/v1/downloads/{task_id}/body")
    async def download_body(task_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        try:
            task, path = await downloads.acquire_body(task_id)
        except DownloadError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return StreamingResponse(
            downloads.stream_file(task, path),
            media_type="application/octet-stream",
            headers={"Content-Length": str(task.total_size)},
        )

    @app.delete("/v1/downloads/{task_id}")
    async def delete_download(task_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        await downloads.delete(task_id)
        return {"status": "DELETED", "task_id": task_id}

    @app.get("/v1/metrics")
    async def metrics(authorization: str | None = Header(default=None)):
        authorize(authorization)
        state.prune()
        return {
            **state.metrics,
            "cache_entry_count": len(state.store.entries),
            "retained_result_bytes": state.retained_result_bytes,
            "execution_record_count": len(state.executions),
            "active_execution_count": sum(
                1 for item in state.executions.values()
                if item["status"] in ("QUEUED", "RUNNING", "RETRY_WAIT")
            ),
            "downloads": downloads.metrics(),
        }

    @app.get("/v1/executions/{execution_id}")
    async def execution(execution_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        result = state.executions.get(execution_id)
        if result is None:
            raise HTTPException(status_code=404, detail="unknown execution")
        return {k: v for k, v in result.items() if k != "request"} | {"execution_id": execution_id}

    return app


async def _cleanup_loop(state: State) -> None:
    while True:
        await asyncio.sleep(1)
        state.store.cleanup()
        state.prune()
        await state.downloads.cleanup()

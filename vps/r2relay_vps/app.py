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
from pydantic import BaseModel, Field

from .cache import CacheStore, MD5_PATTERN
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
               retry_delays: tuple[float, ...] = (2, 4, 8)) -> FastAPI:
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

    app = FastAPI(title="R2 Relay VPS", lifespan=lifespan)
    state = State(store or CacheStore(Path(os.environ.get("R2_RELAY_CACHE_DIR", "/var/lib/r2-relay/cache"))),
                  token if token is not None else os.environ.get("R2_RELAY_TOKEN", ""),
                  R2Worker(transport=r2_transport, delays=retry_delays))
    if not state.token:
        raise RuntimeError("R2_RELAY_TOKEN must be configured")
    app.state.relay = state

    def authorize(authorization: str | None):
        if not authorization or not secrets.compare_digest(authorization, f"Bearer {state.token}"):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/parts/prepare")
    async def prepare(p: Prepare, authorization: str | None = Header(default=None)):
        authorize(authorization)
        if p.schema_version != 1 or not MD5_PATTERN.fullmatch(p.md5):
            raise HTTPException(status_code=400, detail="invalid schema or md5")
        u = urlsplit(p.r2_request.url)
        query = parse_qs(u.query)
        if (p.r2_request.method != "PUT" or u.scheme != "https" or u.port not in (None, 443)
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

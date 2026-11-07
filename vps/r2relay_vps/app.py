"""HTTP cache negotiation endpoints. R2 upload worker is a separate M1 task."""
from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .cache import CacheStore, MD5_PATTERN


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
    def __init__(self, store: CacheStore, token: str):
        self.store = store
        self.token = token
        self.transfers: dict[str, Prepare] = {}
        self.executions: dict[str, dict] = {}


def create_app(store: CacheStore | None = None, token: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_cleanup_loop(app.state.relay.store))
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="R2 Relay VPS", lifespan=lifespan)
    state = State(store or CacheStore(Path(os.environ.get("R2_RELAY_CACHE_DIR", "/var/lib/r2-relay/cache"))),
                  token if token is not None else os.environ.get("R2_RELAY_TOKEN", ""))
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
        if (p.r2_request.method != "PUT" or u.scheme != "https"
                or not u.hostname or not u.hostname.endswith(".r2.cloudflarestorage.com")
                or u.hostname == ".r2.cloudflarestorage.com"
                or "uploadId" not in query or "partNumber" not in query):
            raise HTTPException(status_code=400, detail="invalid R2 UploadPart target")
        execution_id = secrets.token_urlsafe(20)
        state.executions[execution_id] = {"status": "QUEUED", "request": p.model_dump()}
        if state.store.get(p.md5) is not None:
            # The next M1 iteration supplies actual R2 workers. Never report 200 here.
            return {"status": "HIT", "execution_id": execution_id}
        transfer_id = secrets.token_urlsafe(20)
        state.transfers[transfer_id] = p
        state.executions[execution_id]["transfer_id"] = transfer_id
        return {"status": "MISS", "transfer_id": transfer_id, "execution_id": execution_id}

    @app.put("/v1/parts/data/{transfer_id}")
    async def upload_data(transfer_id: str, request: Request,
                          authorization: str | None = Header(default=None),
                          x_content_md5_hex: str | None = Header(default=None)):
        authorize(authorization)
        p = state.transfers.get(transfer_id)
        if p is None:
            raise HTTPException(status_code=404, detail="unknown transfer")
        if x_content_md5_hex != p.md5:
            raise HTTPException(status_code=400, detail="md5 metadata mismatch")
        try:
            entry = await state.store.put(p.md5, request.stream(), p.size)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            state.transfers.pop(transfer_id, None)
        return {"status": "STAGED", "md5": entry.md5, "size": entry.size}

    @app.get("/v1/executions/{execution_id}")
    async def execution(execution_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        result = state.executions.get(execution_id)
        if result is None:
            raise HTTPException(status_code=404, detail="unknown execution")
        return {"status": result["status"], "execution_id": execution_id}

    return app


async def _cleanup_loop(store: CacheStore) -> None:
    while True:
        await asyncio.sleep(1)
        store.cleanup()

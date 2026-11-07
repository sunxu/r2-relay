"""Local HTTPS VPS service with injected fake R2 transport. Never reaches Cloudflare."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx

from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore

calls = Path(os.environ["R2_TEST_CALL_LOG"])
attempts: dict[str, int] = {}


def fake_r2(request: httpx.Request) -> httpx.Response:
    # A request reaches here only from the worker. This transport is offline.
    key = str(request.url)
    attempts[key] = attempts.get(key, 0) + 1
    entry = {
        "url": key,
        "length": len(request.content),
        "body_md5": hashlib.md5(request.content, usedforsecurity=False).hexdigest(),
        "host": request.headers.get("host"),
        "content_length": request.headers.get("content-length"),
        "attempt": attempts[key],
    }
    with calls.open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry) + "\n")
    if attempts[key] == 1 and "uploadId=first" in key:
        return httpx.Response(502, text="simulated R2 transient failure")
    return httpx.Response(200, headers={"ETag": '"' + entry["body_md5"] + '"'})


app = create_app(
    CacheStore(Path(os.environ["R2_TEST_CACHE_DIR"])),
    token=os.environ["R2_RELAY_TOKEN"],
    r2_transport=httpx.MockTransport(fake_r2),
    retry_delays=(0, 0, 0),
)

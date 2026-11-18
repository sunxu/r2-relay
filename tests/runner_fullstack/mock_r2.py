"""Networked mock R2. Only runs in an isolated GitHub Runner.
No Cloudflare credentials, no external requests and no production SigV4 claims.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request, Response

app = FastAPI()
attempts = {}


@app.get("/healthz")
async def healthz():
    return {"status": "mock-r2-online"}


@app.put("/mock/uploadpart")
async def upload_part(request: Request):
    original = request.headers.get("x-mock-original-url", "")
    source = urlsplit(original)
    if source.scheme != "https" or not (source.hostname or "").endswith(
        ".r2.cloudflarestorage.com"
    ):
        raise HTTPException(400, "original target was not an R2 UploadPart URL")
    query = parse_qs(source.query)
    upload_id = query.get("uploadId", [""])[0]
    part_number = query.get("partNumber", [""])[0]
    if not upload_id or not part_number.isdecimal():
        raise HTTPException(400, "missing UploadPart identifiers")
    # Independent HMAC witness ensures route, part number and bytes survive the
    # proxy/relay. This is NOT AWS SigV4 authentication.
    body = await request.body()
    digest = hashlib.md5(body, usedforsecurity=False).hexdigest()
    witness = hmac.new(
        os.environ["R2_TEST_HMAC_KEY"].encode(),
        f"{upload_id}|{part_number}|{digest}".encode(), "sha256"
    ).hexdigest()
    supplied = query.get("X-Test-Witness", [""])[0]
    if not hmac.compare_digest(witness, supplied):
        raise HTTPException(403, "test witness mismatch")
    name = f"{upload_id}:{part_number}"
    attempts[name] = attempts.get(name, 0) + 1
    count = attempts[name]
    log = Path(os.environ["R2_TEST_CALL_LOG"])
    entry = {
        "upload_id": upload_id, "part_number": part_number, "attempt": count,
        "body_md5": digest, "bytes": len(body),
        "signed_host": request.headers.get("x-mock-original-host"),
    }
    with log.open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry, sort_keys=True) + "\n")
    if upload_id == "fail502" and count == 1:
        return Response("simulated 502", status_code=502)
    if upload_id == "fail429" and count == 1:
        return Response("simulated 429", status_code=429)
    if upload_id == "forbidden":
        return Response("simulated 403", status_code=403)
    return Response(status_code=200, headers={"ETag": f'"{digest}"'})


@app.get("/mock/download")
async def download_object(request: Request):
    original = request.headers.get("x-mock-original-url", "")
    source = urlsplit(original)
    if source.scheme != "https" or not (source.hostname or "").endswith(
        ".r2.cloudflarestorage.com"
    ):
        raise HTTPException(400, "original target was not an R2 GET URL")
    if request.headers.get("x-mock-original-host") != source.hostname:
        raise HTTPException(400, "original Host was not preserved")
    if request.headers.get("x-test-signed-marker") != "signed-get-marker":
        raise HTTPException(403, "signed GET marker was not preserved")

    total = 25 * 1024 * 1024
    raw_range = request.headers.get("range", "")
    if not raw_range.startswith("bytes=") or "," in raw_range:
        raise HTTPException(416, "this test requires one explicit byte range")
    try:
        first, last = raw_range[6:].split("-", 1)
        start = int(first)
        end = min(int(last), total - 1)
    except (ValueError, TypeError):
        raise HTTPException(416, "invalid byte range")
    if start < 0 or end < start or start >= total:
        raise HTTPException(416, "range outside mock object")

    async def chunks():
        offset = start
        while offset <= end:
            stop = min(offset + 1024 * 1024, end + 1)
            yield bytes((index % 251 for index in range(offset, stop)))
            offset = stop

    from fastapi.responses import StreamingResponse

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Range": f"bytes {start}-{end}/{total}",
        "ETag": '"mock-get-v1"',
        "Content-Type": "application/octet-stream",
        "X-Mock-Signed-Marker": request.headers["x-test-signed-marker"],
    }
    log = Path(os.environ["R2_TEST_GET_LOG"])
    with log.open("a", encoding="utf-8") as out:
        out.write(json.dumps({
            "original_url": original,
            "original_host": request.headers.get("x-mock-original-host"),
            "range": raw_range,
            "signed_marker": request.headers.get("x-test-signed-marker"),
            "authorization_present": bool(request.headers.get("authorization")),
        }, sort_keys=True) + "\\n")
    return StreamingResponse(
        chunks(), status_code=206, headers=headers,
        media_type="application/octet-stream",
    )

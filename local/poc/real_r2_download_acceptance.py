#!/usr/bin/env python3
"""Run one real signed R2 GET through the deployed VPS download relay.

The private request specification is read from a mode-0600 JSON file. Neither
its URL nor its headers are printed or written to the acceptance report.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from r2relay_local.config import load_settings
from r2relay_local.download import DownloadCoordinator
from r2relay_local.relay import RelayClient


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def load_spec() -> dict:
    raw_path = os.environ.get("R2_RELAY_REAL_GET_SPEC", "")
    if not raw_path:
        raise ValueError("R2_RELAY_REAL_GET_SPEC is required")
    path = Path(raw_path).expanduser()
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("private request spec must have mode 0600")
    data = json.loads(path.read_text(encoding="utf-8"))
    if set(data) != {
        "url", "headers", "expected_status_code", "expected_size", "expected_sha256"
    }:
        raise ValueError("request spec has missing or unknown fields")

    url = data["url"]
    if not isinstance(url, str) or len(url) > 16384:
        raise ValueError("invalid signed R2 URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or not (parsed.hostname or "").endswith(".r2.cloudflarestorage.com")
    ):
        raise ValueError("URL must target a Cloudflare R2 S3 HTTPS endpoint")

    headers = data["headers"]
    if not isinstance(headers, list) or len(headers) > 128:
        raise ValueError("headers must be a list of at most 128 pairs")
    normalized = []
    for pair in headers:
        if (
            not isinstance(pair, list) or len(pair) != 2
            or not all(isinstance(value, str) for value in pair)
            or any("\r" in value or "\n" in value for value in pair)
        ):
            raise ValueError("invalid request header pair")
        normalized.append((pair[0], pair[1]))

    status = data["expected_status_code"]
    size = data["expected_size"]
    digest = data["expected_sha256"]
    if type(status) is not int or not 200 <= status <= 599:
        raise ValueError("expected_status_code must be an HTTP response status")
    if type(size) is not int or size < 0:
        raise ValueError("expected_size must be a non-negative integer")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError("expected_sha256 must be a 64-character SHA-256 hex digest")
    return {
        "url": url,
        "headers": normalized,
        "expected_status_code": status,
        "expected_size": size,
        "expected_sha256": digest.lower(),
    }


def hash_result(result) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    if result.body is not None:
        digest.update(result.body)
        size = len(result.body)
    elif result.path is not None:
        with result.path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    else:
        raise ValueError("download result has no response body source")
    return size, digest.hexdigest()


async def run() -> int:
    relay = None
    coordinator = None
    result = None
    started = time.perf_counter()
    try:
        spec = load_spec()
        settings = load_settings()
        if not settings.download_enabled:
            raise ValueError("local download relay is disabled in TOML")
        endpoint = os.environ.get("R2_RELAY_VPS_URL", "")
        token = os.environ.get("R2_RELAY_TOKEN", "")
        if not endpoint or not token:
            raise ValueError("R2_RELAY_VPS_URL and R2_RELAY_TOKEN are required")

        relay = RelayClient(endpoint, token)
        coordinator = DownloadCoordinator(relay, settings)
        result = await coordinator.download(spec["url"], spec["headers"])
        actual_size, actual_sha256 = hash_result(result)
        status_ok = result.status_code == spec["expected_status_code"]
        size_ok = actual_size == spec["expected_size"]
        digest_ok = actual_sha256 == spec["expected_sha256"]
        remote_cleanup_ok = coordinator.remote_cleanup_failures == 0
        actual_status = result.status_code
        http_versions = sorted(set(relay.download_http_versions))
        local_path = result.path
        if result.cleanup is not None:
            await result.cleanup()
        local_temp_cleanup_ok = (
            (local_path is None or not local_path.exists())
            and coordinator.disk_budget.used == 0
            and coordinator.memory_budget.used == 0
        )
        passed = (
            status_ok and size_ok and digest_ok
            and remote_cleanup_ok and local_temp_cleanup_ok
        )
        result = None
        await coordinator.close()
        coordinator = None
        await relay.close()
        relay = None
        report = {
            "schema": "r2_relay_real_get_acceptance_v1",
            "status": "PASS_REAL" if passed else "FAIL_REAL",
            "http_status": actual_status,
            "expected_status": spec["expected_status_code"],
            "actual_size": actual_size,
            "expected_size": spec["expected_size"],
            "sha256": actual_sha256,
            "expected_sha256_match": digest_ok,
            "size_match": size_ok,
            "status_match": status_ok,
            "vps_task_cleanup_ok": remote_cleanup_ok,
            "local_temp_cleanup_ok": local_temp_cleanup_ok,
            "mac_to_vps_http_versions": http_versions,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "signed_url_or_headers_logged": False,
        }
        print(json.dumps(report, sort_keys=True, indent=2))
        return 0 if passed else 1
    except Exception as exc:
        # Do not print exception messages: HTTP client errors can contain a
        # signed URL or request metadata.
        print(json.dumps({
            "schema": "r2_relay_real_get_acceptance_v1",
            "status": "FAIL_REAL",
            "error_type": type(exc).__name__,
            "signed_url_or_headers_logged": False,
        }, sort_keys=True, indent=2))
        return 1
    finally:
        if result is not None and result.cleanup is not None:
            try:
                await result.cleanup()
            except Exception:
                pass
        if coordinator is not None:
            try:
                await coordinator.close()
            except Exception:
                pass
        if relay is not None:
            try:
                await relay.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))

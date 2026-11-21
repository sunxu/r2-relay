#!/usr/bin/env python3
"""Validate a real signed R2 Range GET through isolated Mac mitmproxy.

Reads the local private acceptance spec (0600), never prints URL/headers,
and streams the response to SHA-256 without making a second full-size copy.
Run only during the Receiver's explicitly enabled 30-minute test lease.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_PROXY = "http://127.0.0.1:18586"


def load_private_spec() -> dict:
    raw = os.environ.get("R2_RELAY_REAL_GET_SPEC", "")
    if not raw:
        raise ValueError("missing_private_spec")
    path = Path(raw).expanduser()
    if path.is_symlink():
        raise ValueError("spec_symlink_not_allowed")
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("private_spec_must_be_0600")
    data = json.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"url", "headers", "expected_status_code", "expected_size",
                     "expected_sha256", "expected_headers"}:
        raise ValueError("invalid_private_spec_fields")
    url = data["url"]
    if not isinstance(url, str) or len(url) > 16384:
        raise ValueError("invalid_signed_url")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.port not in (None, 443)
            or not (parsed.hostname or "").endswith(".r2.cloudflarestorage.com")
            or parsed.username or parsed.password):
        raise ValueError("invalid_r2_endpoint")
    headers = data["headers"]
    if (not isinstance(headers, list) or len(headers) > 128
            or any(not isinstance(pair, list) or len(pair) != 2
                   or any(not isinstance(v, str) or "\r" in v or "\n" in v for v in pair)
                   for pair in headers)):
        raise ValueError("invalid_request_headers")
    if (type(data["expected_status_code"]) is not int
            or data["expected_status_code"] != 206
            or type(data["expected_size"]) is not int
            or not 0 < data["expected_size"] <= 64 * 1024 * 1024
            or not isinstance(data["expected_sha256"], str)
            or not _SHA256.fullmatch(data["expected_sha256"].lower())):
        raise ValueError("invalid_expected_response")
    expected = data["expected_headers"]
    if (not isinstance(expected, dict)
            or not {"content-range", "content-length"} <= {x.lower() for x in expected}
            or any(not isinstance(k, str) or not isinstance(v, str)
                   or "\r" in k or "\n" in k or "\r" in v or "\n" in v
                   for k, v in expected.items())):
        raise ValueError("invalid_expected_headers")
    return data


def check_response(spec: dict, *, status: int, headers: httpx.Headers,
                   size: int, digest: str) -> dict:
    expected_headers = {k.lower(): v for k, v in spec["expected_headers"].items()}
    checks = {k: headers.get(k) == v for k, v in expected_headers.items()}
    checks_ok = all(checks.values()) and bool(checks)
    status_ok = status == spec["expected_status_code"]
    size_ok = size == spec["expected_size"]
    digest_ok = digest == spec["expected_sha256"].lower()
    ok = checks_ok and status_ok and size_ok and digest_ok
    return {
        "schema": "r2_relay_real_proxy_acceptance_v1",
        "status": "PASS_REAL_PROXY" if ok else "FAIL_REAL_PROXY",
        "http_status": status,
        "expected_status": spec["expected_status_code"],
        "actual_size": size,
        "expected_size": spec["expected_size"],
        "sha256_match": digest_ok,
        "status_match": status_ok,
        "size_match": size_ok,
        "expected_headers_match": checks_ok,
        "header_checks": checks,
        "proxy": "127.0.0.1:18586",
        "signed_url_or_headers_logged": False,
    }


def run() -> int:
    started = time.perf_counter()
    try:
        spec = load_private_spec()
        proxy = os.environ.get("R2_RELAY_TEST_PROXY", _ALLOWED_PROXY)
        if proxy != _ALLOWED_PROXY:
            raise ValueError("proxy_must_be_isolated_18586")
        ca_raw = os.environ.get("R2_RELAY_MITMPROXY_CA", "~/.mitmproxy/mitmproxy-ca-cert.pem")
        ca_path = Path(ca_raw).expanduser()
        if not ca_path.is_file():
            raise FileNotFoundError("mitmproxy_ca_missing")
        hasher = hashlib.sha256()
        size = 0
        header_elapsed_ms = None
        first_body_elapsed_ms = None
        first_16mib_elapsed_ms = None
        with httpx.Client(proxy=proxy, verify=str(ca_path), timeout=120,
                          follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", spec["url"], headers=spec["headers"]) as response:
                header_elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                status = response.status_code
                # R2 acceptance compares the wire representation, not auto-decompressed bytes.
                # IOTA reads up to 64 KiB per iteration. Use the same probe
                # granularity instead of delaying first-body timing by 1 MiB.
                for chunk in response.iter_raw(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if first_body_elapsed_ms is None:
                        first_body_elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise ValueError("response_over_64_mib")
                    if size >= 16 * 1024 * 1024 and first_16mib_elapsed_ms is None:
                        first_16mib_elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                    hasher.update(chunk)
                report = check_response(spec, status=status, headers=response.headers,
                                        size=size, digest=hasher.hexdigest())
        report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        report["header_elapsed_ms"] = header_elapsed_ms
        report["first_body_elapsed_ms"] = first_body_elapsed_ms
        report["first_16mib_elapsed_ms"] = first_16mib_elapsed_ms
        # Only real expected R2 response headers count towards IOTA's
        # replacement-open budget. A quickly generated local 502 is not a pass.
        report["header_before_iota_20s_timeout"] = (
            report["status_match"]
            and report["expected_headers_match"]
            and header_elapsed_ms is not None
            and header_elapsed_ms < 20000
        )
        print(json.dumps(report, sort_keys=True, indent=2))
        return 0 if report["status"] == "PASS_REAL_PROXY" else 1
    except Exception as exc:
        # Never emit exception messages: HTTP clients may include the signed URL.
        print(json.dumps({
            "schema": "r2_relay_real_proxy_acceptance_v1",
            "status": "FAIL_REAL_PROXY",
            "error_type": type(exc).__name__,
            "signed_url_or_headers_logged": False,
        }, sort_keys=True, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(run())

#!/usr/bin/env python3
"""Offline extraction of private IOTA R2 GET captures for relay acceptance.

No network calls; never prints signed URLs, headers, or credentials. An exported
request is *not* proof that the remote R2 object or its signature is usable.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import parse_qs, urlsplit

MIB = 1024 * 1024
THRESHOLD = 24 * MIB
MAX_RANGE = 64 * MIB  # Bound requests intended for the real-R2 acceptance spec.
RANGE_RE = re.compile(r"bytes=(\d+)-(\d+)\Z", re.IGNORECASE)
ID_RE = re.compile(r"[0-9a-f]{16}\Z", re.IGNORECASE)
SIG_RE = re.compile(r"[0-9a-f]{64}\Z", re.IGNORECASE)
REQUEST_SCHEMA = "iota_r2_download_request_v1"
EVENT_SCHEMA = "iota_r2_download_event_v1"
CONDITIONAL = frozenset({
    "if-range", "if-match", "if-none-match", "if-modified-since", "if-unmodified-since",
})


class PrivateInputError(Exception):
    """Only fixed, non-sensitive error codes are exposed to the CLI."""


def read_jsonl(path: Path):
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_file():
        raise PrivateInputError("INPUT_NOT_REGULAR")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise PrivateInputError("INPUT_NOT_PRIVATE")
    # O_NOFOLLOW avoids opening a replacement symlink after the type check.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(fd).st_mode) or stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
        os.close(fd)
        raise PrivateInputError("INPUT_NOT_PRIVATE_REGULAR")
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except ValueError:
                continue  # Ignore truncated or malformed individual log lines.
            if isinstance(value, dict):
                yield value


def validate_request(row: dict):
    if (row.get("schema") != REQUEST_SCHEMA or row.get("method") != "GET"
            or not isinstance(row.get("request_id"), str)
            or not ID_RE.fullmatch(row["request_id"])):
        return None, "INVALID_REQUEST"
    url = row.get("url")
    if not isinstance(url, str) or not 0 < len(url) <= 16384:
        return None, "INVALID_URL"
    try:
        p = urlsplit(url)
        if (p.scheme != "https" or p.username is not None or p.password is not None
                or p.port not in (None, 443) or p.fragment
                or not (p.hostname or "").lower().endswith(".r2.cloudflarestorage.com")):
            return None, "NOT_R2_HTTPS"
        query = parse_qs(p.query, keep_blank_values=True)
    except (ValueError, TypeError):
        return None, "INVALID_URL"
    headers = row.get("headers")
    if (not isinstance(headers, list) or len(headers) > 128
            or sum(len(str(x)) for pair in headers if isinstance(pair, list) for x in pair) > 65536
            or any(not isinstance(pair, list) or len(pair) != 2
                   or any(not isinstance(x, str) or "\r" in x or "\n" in x for x in pair)
                   for pair in headers)):
        return None, "INVALID_HEADERS"
    host = [v for k, v in headers if k.lower() == "host"]
    if len(host) > 1 or (host and host[0].lower() != p.netloc.lower()):
        return None, "HOST_MISMATCH"
    if (query.get("X-Amz-Algorithm") != ["AWS4-HMAC-SHA256"]
            or len(query.get("X-Amz-Credential", [])) != 1
            or len(query.get("X-Amz-Signature", [])) != 1
            or not SIG_RE.fullmatch(query["X-Amz-Signature"][0])):
        return None, "NOT_QUERY_SIGV4"
    try:
        if len(query.get("X-Amz-Date", [])) != 1 or len(query.get("X-Amz-Expires", [])) != 1:
            return None, "EXPIRY_UNKNOWN"
        issued = datetime.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        lifetime = int(query["X-Amz-Expires"][0])
        if not 1 <= lifetime <= 604800:
            return None, "EXPIRY_UNKNOWN"
        expires_at = issued + timedelta(seconds=lifetime)
    except (TypeError, ValueError, OverflowError):
        return None, "EXPIRY_UNKNOWN"
    return {
        "row": row, "request_id": row["request_id"], "headers": headers,
        "query": query, "expiry": expires_at,
        "session_token": "X-Amz-Security-Token" in query,
    }, "OK"


def range_size(headers: list):
    ranges = [v for k, v in headers if k.lower() == "range"]
    if len(ranges) != 1:
        return None
    match = RANGE_RE.fullmatch(ranges[0].strip())
    if match is None:
        return None
    start, end = map(int, match.groups())
    return end - start + 1 if end >= start else None


def classify(info: dict, summary: dict | None, now: datetime, min_ttl: int):
    ttl = int((info["expiry"] - now).total_seconds())
    if ttl <= 0:
        return None, None, "EXPIRED", "EXPIRED"
    if ttl < min_ttl * 60:
        return None, None, "EXPIRING_SOON", "EXPIRING_SOON"
    info = {**info, "ttl_minutes": ttl // 60, "timestamp": info["row"].get("timestamp")}
    observed = summary.get("response_content_length") if summary else None
    if type(observed) is not int or observed < 0:
        observed = None
    info["observed_bytes"] = observed
    raw_status = summary.get("response_status") if summary else None
    info["prior_status"] = raw_status if type(raw_status) is int and 100 <= raw_status <= 599 else None
    info["prior_complete"] = bool(summary and summary.get("final_status") == "completed")
    raw_takeover = summary.get("takeover_result") if summary else None
    info["takeover"] = raw_takeover if isinstance(raw_takeover, str) and raw_takeover in {
        "completed", "succeeded", "failed", "cancelled", "stream_failed",
        "stalled", "not_attempted", "not_attempted_capacity_full",
    } else None
    info["range_bytes"] = range_size(info["headers"])

    strict, strict_reason = None, "NO_EVENT_SUMMARY"
    if summary:
        if not info["prior_complete"] or info["prior_status"] != 206:
            strict_reason = "NOT_COMPLETE_206"
        elif observed is None or observed <= THRESHOLD:
            strict_reason = "NOT_OVER_24_MIB"
        elif info["range_bytes"] is None or info["range_bytes"] != observed:
            strict_reason = "RANGE_LENGTH_MISMATCH"
        else:
            strict, strict_reason = info, "OK"

    # Retaining a signature whose only signed header is Host permits an
    # independent verifier to change Range without invalidating SigV4.
    replay, replay_reason = None, "OK"
    if info["query"].get("X-Amz-SignedHeaders") != ["host"]:
        replay_reason = "SIGNED_HEADERS_NOT_HOST_ONLY"
    elif any(k.lower() in CONDITIONAL for k, _ in info["headers"]):
        replay_reason = "CONDITIONAL_HEADERS"
    elif sum(k.lower() == "range" for k, _ in info["headers"]) > 1:
        replay_reason = "MULTIPLE_RANGE_HEADERS"
    else:
        replay = info
    return strict, replay, strict_reason, replay_reason


def load_candidates(directory: Path, now: datetime, min_ttl: int):
    if directory.is_symlink() or not directory.is_dir():
        raise PrivateInputError("LOG_DIRECTORY_MISSING_OR_SYMLINK")
    if stat.S_IMODE(directory.stat().st_mode) & 0o077:
        raise PrivateInputError("LOG_DIRECTORY_NOT_PRIVATE")
    event = directory / "r2-download-events.jsonl"
    requests = [directory / "r2-download-requests.jsonl.1", directory / "r2-download-requests.jsonl"]
    if not any(x.is_file() for x in requests):
        raise PrivateInputError("REQUEST_LOG_MISSING")
    summaries = {}
    for row in read_jsonl(event):
        if (row.get("schema") == EVENT_SCHEMA and row.get("result") == "download_summary"
                and isinstance(row.get("request_id"), str)):
            summaries[row["request_id"]] = row

    strict, replays = {}, {}
    strict_counts, replay_counts = Counter(), Counter()
    identities = {}  # Reject ambiguous duplicate IDs across rotation.
    ambiguous = set()
    for path in requests:
        for row in read_jsonl(path):
            info, reason = validate_request(row)
            if info is None:
                strict_counts[reason] += 1
                replay_counts[reason] += 1
                continue
            key = info["request_id"]
            identity = (info["row"]["url"], json.dumps(info["headers"], ensure_ascii=True))
            if key in identities and identities[key] != identity:
                ambiguous.add(key)
            identities[key] = identity
            match, replay, why_strict, why_replay = classify(info, summaries.get(key), now, min_ttl)
            strict_counts[why_strict] += 1
            replay_counts[why_replay] += 1
            if match is not None:
                strict[key] = match
            if replay is not None:
                replays[key] = replay
    for key in ambiguous:
        strict.pop(key, None)
        replays.pop(key, None)
    return strict, replays, strict_counts, replay_counts


def export_private(path: Path, request: dict, new_range: str | None):
    directory = path.parent
    if directory.is_symlink():
        raise PrivateInputError("OUTPUT_DIRECTORY_SYMLINK")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or stat.S_IMODE(directory.stat().st_mode) & 0o077:
        raise PrivateInputError("OUTPUT_DIRECTORY_NOT_PRIVATE")
    headers = [list(pair) for pair in request["headers"]]
    if new_range is not None:
        found = False
        for pair in headers:
            if pair[0].lower() == "range":
                pair[1] = new_range
                found = True
        if not found:
            headers.append(["Range", new_range])
    payload = {"method": "GET", "url": request["row"]["url"], "headers": headers}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=True, separators=(",", ":"))
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main(argv=None, *, now=None):
    parser = argparse.ArgumentParser(description="离线筛选 IOTA 私有 R2 GET 日志；绝不输出签名 URL 或请求头")
    parser.add_argument("--dir", type=Path, default=Path.home() / "Library/Application Support/iota-tools")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--min-ttl-minutes", type=int, default=30)
    parser.add_argument("--select", metavar="REQUEST_ID")
    parser.add_argument("--out", type=Path, help="新建的私有0600请求JSON（不允许覆盖）")
    parser.add_argument("--new-range", help="仅host签名时可变更Range，如 bytes=0-25301338")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100 or not 0 <= args.min_ttl_minutes <= 10080:
        parser.error("INVALID_LIMIT_OR_TTL")
    if bool(args.select) != bool(args.out):
        parser.error("SELECT_REQUIRES_OUT")
    if args.new_range is not None:
        match = RANGE_RE.fullmatch(args.new_range)
        if not args.select or match is None:
            parser.error("RANGE_REQUIRES_SELECT")
        start, end = map(int, match.groups())
        if end < start or not THRESHOLD < end - start + 1 <= MAX_RANGE:
            parser.error("RANGE_MUST_BE_MORE_THAN_24_AND_AT_MOST_64_MIB")

    now = now or datetime.now(timezone.utc)
    try:
        strict, replays, counts, replay_counts = load_candidates(args.dir.expanduser(), now, args.min_ttl_minutes)
        strict_rows = sorted(strict.values(), key=lambda r: (r["ttl_minutes"], r["observed_bytes"] or 0), reverse=True)
        replay_rows = sorted(replays.values(), key=lambda r: (
            r["prior_complete"] and (r["observed_bytes"] or 0) > THRESHOLD,
            r["observed_bytes"] or 0, r["ttl_minutes"],
        ), reverse=True)
        print("SAFE_CANDIDATES:", len(strict_rows))
        print("REJECTION_COUNTS:", " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        print("request_id        size_MiB  remaining_min  takeover  temporary_token")
        for row in strict_rows[:args.limit]:
            print(f'{row["request_id"]:17} {row["observed_bytes"]/MIB:8.2f} {row["ttl_minutes"]:14} '
                  f'{str(row["takeover"] or "-"):9} {"YES" if row["session_token"] else "NO"}')
        print("REPLAY_CANDIDATES:", len(replay_rows))
        print("REPLAY_REJECTION_COUNTS:", " ".join(f"{k}={v}" for k, v in sorted(replay_counts.items())))
        print("request_id        remaining_min  observed_MiB  range_MiB  prior_status  session_token")
        for row in replay_rows[:args.limit]:
            observed = f'{row["observed_bytes"]/MIB:.2f}' if row["observed_bytes"] is not None else "unknown"
            extent = f'{row["range_bytes"]/MIB:.2f}' if row["range_bytes"] is not None else "unknown"
            print(f'{row["request_id"]:17} {row["ttl_minutes"]:13} {observed:>13} {extent:>10} '
                  f'{str(row["prior_status"] or "-"):>13} {"YES" if row["session_token"] else "NO"}')
        if args.select is not None:
            if not ID_RE.fullmatch(args.select):
                raise PrivateInputError("INVALID_SELECTION")
            selected = (replays.get(args.select) if args.new_range else
                        strict.get(args.select) or replays.get(args.select))
            if selected is None:
                raise PrivateInputError("SELECTION_NOT_ELIGIBLE")
            export_private(args.out.expanduser(), selected, args.new_range)
            print("PRIVATE_REQUEST_EXPORTED: YES (new file, mode 0600)")
            print("RANGE_CHANGED:", args.new_range is not None)
            print("NEXT: independently verify direct R2 status, Content-Range, size and SHA-256")
        return 0
    except (PrivateInputError, OSError, ValueError) as exc:
        # Exceptions from OS/json/url parsing may include signed URLs or file
        # paths; emit only fixed error codes or exception types, never messages.
        code = str(exc) if isinstance(exc, PrivateInputError) else type(exc).__name__
        print(f"HOLD: {code}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

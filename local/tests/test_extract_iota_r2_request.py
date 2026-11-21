"""Secret-safe regression tests for the standalone IOTA R2 request extractor."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import stat
from urllib.parse import urlencode

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "extract_iota_r2_request.py"
spec = importlib.util.spec_from_file_location("extract_iota_r2_request", MODULE_PATH)
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)

NOW = datetime(2026, 11, 21, 8, tzinfo=timezone.utc)
ID1 = "1" * 16
ID2 = "2" * 16
SIZE = 25 * 1024 * 1024


def signed_url(*, signed_headers="host", signed_date="20261121T080000Z", expires=3600, token=False):
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": "TEST/20261121/auto/s3/aws4_request",
        "X-Amz-Date": signed_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": signed_headers,
        "X-Amz-Signature": "f" * 64,
    }
    if token:
        query["X-Amz-Security-Token"] = "PRIVATE_TOKEN_NEVER_PRINT"
    return "https://abc.r2.cloudflarestorage.com/bucket/object?" + urlencode(query)


def write_log(path, entries):
    path.write_text("".join(json.dumps(x) + "\n" for x in entries), encoding="utf-8")
    path.chmod(0o600)


def request(*, request_id=ID1, url=None, length=12 * 1024 * 1024, headers=None):
    return {
        "schema": "iota_r2_download_request_v1", "request_id": request_id,
        "method": "GET", "timestamp": NOW.timestamp(),
        "url": url or signed_url(),
        "headers": headers if headers is not None else [["Range", f"bytes=0-{length - 1}"]],
        "replacement_proxy": "http://127.0.0.1:18585",
    }


def summary(*, request_id=ID1, size=SIZE, status=206, final="completed"):
    return {
        "schema": "iota_r2_download_event_v1", "request_id": request_id,
        "result": "download_summary", "final_status": final,
        "response_status": status, "response_content_length": size,
        "takeover_result": "completed",
    }


def private_logs(tmp_path, reqs, events=None, *, rotated=None):
    directory = tmp_path / "iota-runtime"
    directory.mkdir(mode=0o700)
    write_log(directory / "r2-download-requests.jsonl", reqs)
    if events is not None:
        write_log(directory / "r2-download-events.jsonl", events)
    if rotated is not None:
        write_log(directory / "r2-download-requests.jsonl.1", rotated)
    return directory


def test_strict_completed_206_range_and_offline_replay_selection(tmp_path, capsys):
    url = signed_url(token=True)
    runtime = private_logs(tmp_path, [request(url=url, length=SIZE)], [summary()])
    out = tmp_path / "private" / "selected.json"
    assert extract.main(["--dir", str(runtime), "--select", ID1, "--out", str(out)], now=NOW) == 0
    shown = capsys.readouterr().out
    assert "SAFE_CANDIDATES: 1" in shown
    assert "REPLAY_CANDIDATES: 1" in shown
    assert "PRIVATE_REQUEST_EXPORTED: YES" in shown
    assert url not in shown and "PRIVATE_TOKEN_NEVER_PRINT" not in shown
    assert "X-Amz-Signature" not in shown
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.parent.stat().st_mode) == 0o700
    assert json.loads(out.read_text())["url"] == url


def test_replay_without_event_summary_can_change_unsigned_range(tmp_path, capsys):
    runtime = private_logs(tmp_path, [request()], events=None)
    out = tmp_path / "private" / "selected.json"
    args = ["--dir", str(runtime), "--select", ID1, "--out", str(out),
            "--new-range", f"bytes=0-{SIZE-1}"]
    assert extract.main(args, now=NOW) == 0
    shown = capsys.readouterr().out
    assert "SAFE_CANDIDATES: 0" in shown
    assert "NO_EVENT_SUMMARY=1" in shown
    assert "REPLAY_CANDIDATES: 1" in shown
    exported = json.loads(out.read_text())
    assert exported["method"] == "GET"
    assert exported["headers"] == [["Range", f"bytes=0-{SIZE-1}"]]


def test_rotated_request_log_kept_and_expired_filtered(tmp_path, capsys):
    runtime = private_logs(tmp_path,
                           [request(request_id=ID2, url=signed_url(signed_date="20261119T080000Z"))],
                           rotated=[request(request_id=ID1)])
    assert extract.main(["--dir", str(runtime)], now=NOW) == 0
    shown = capsys.readouterr().out
    assert "REPLAY_CANDIDATES: 1" in shown
    assert "EXPIRED=1" in shown
    assert ID1 in shown and ID2 not in shown


def test_signed_range_cannot_be_rewritten(tmp_path, capsys):
    runtime = private_logs(tmp_path, [request(url=signed_url(signed_headers="host;range"))])
    out = tmp_path / "private" / "selected.json"
    assert extract.main(["--dir", str(runtime), "--select", ID1,
                         "--new-range", f"bytes=0-{SIZE-1}", "--out", str(out)], now=NOW) == 1
    assert not out.exists()
    shown = capsys.readouterr().out
    assert "SIGNED_HEADERS_NOT_HOST_ONLY=1" in shown
    assert "HOLD: SELECTION_NOT_ELIGIBLE" in shown
    assert "X-Amz-Credential" not in shown


def test_conditional_and_duplicate_ranges_are_not_replayable(tmp_path, capsys):
    runtime = private_logs(tmp_path, [
        request(request_id=ID1, headers=[["Range", "bytes=0-100"], ["If-Range", '"etag"']]),
        request(request_id=ID2, headers=[["Range", "bytes=0-100"], ["range", "bytes=100-200"]]),
    ])
    assert extract.main(["--dir", str(runtime)], now=NOW) == 0
    shown = capsys.readouterr().out
    assert "REPLAY_CANDIDATES: 0" in shown
    assert "CONDITIONAL_HEADERS=1" in shown
    assert "MULTIPLE_RANGE_HEADERS=1" in shown


def test_reject_existing_file_without_overwrite(tmp_path, capsys):
    runtime = private_logs(tmp_path, [request()])
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    out = private / "selected.json"
    out.write_text("DO_NOT_OVERWRITE")
    out.chmod(0o600)
    assert extract.main(["--dir", str(runtime), "--select", ID1, "--out", str(out)], now=NOW) == 1
    assert out.read_text() == "DO_NOT_OVERWRITE"
    assert "HOLD: FileExistsError" in capsys.readouterr().out


def test_input_must_be_private_and_not_symlink(tmp_path, capsys):
    runtime = private_logs(tmp_path, [request()])
    req = runtime / "r2-download-requests.jsonl"
    req.chmod(0o644)
    assert extract.main(["--dir", str(runtime)], now=NOW) == 1
    assert "HOLD: INPUT_NOT_PRIVATE" in capsys.readouterr().out
    req.chmod(0o600)
    req.rename(runtime / "real.jsonl")
    req.symlink_to(runtime / "real.jsonl")
    assert extract.main(["--dir", str(runtime)], now=NOW) == 1
    assert "HOLD: INPUT_NOT_REGULAR" in capsys.readouterr().out


def test_ambiguous_duplicate_request_ids_never_export(tmp_path, capsys):
    runtime = private_logs(tmp_path, [request(url=signed_url() + "&part=2")],
                           rotated=[request(url=signed_url())])
    out = tmp_path / "private" / "selected.json"
    assert extract.main(["--dir", str(runtime), "--select", ID1, "--out", str(out)], now=NOW) == 1
    shown = capsys.readouterr().out
    assert "REPLAY_CANDIDATES: 0" in shown
    assert "HOLD: SELECTION_NOT_ELIGIBLE" in shown
    assert not out.exists()


def test_reject_host_mismatch_and_bad_signature_safely(tmp_path, capsys):
    runtime = private_logs(tmp_path, [
        request(request_id=ID1, headers=[["Host", "other.example"], ["Range", "bytes=0-500"]]),
        request(request_id=ID2, url="https://abc.r2.cloudflarestorage.com/bucket/private?X-Amz-Signature=SECRET"),
    ])
    assert extract.main(["--dir", str(runtime)], now=NOW) == 0
    shown = capsys.readouterr().out
    assert "REPLAY_CANDIDATES: 0" in shown
    assert "HOST_MISMATCH=1" in shown
    assert "NOT_QUERY_SIGV4=1" in shown
    assert "SECRET" not in shown


def test_output_parent_must_not_be_public_or_symlink(tmp_path, capsys):
    runtime = private_logs(tmp_path, [request()])
    parent = tmp_path / "public"
    parent.mkdir(mode=0o755)
    target = parent / "export.json"
    assert extract.main(["--dir", str(runtime), "--select", ID1, "--out", str(target)], now=NOW) == 1
    assert "HOLD: OUTPUT_DIRECTORY_NOT_PRIVATE" in capsys.readouterr().out
    assert not target.exists()


def test_out_of_range_span_rejected(tmp_path):
    with pytest.raises(SystemExit):
        extract.main(["--select", ID1, "--out", "secret.json", "--new-range", "bytes=0-999"])
    with pytest.raises(SystemExit):
        extract.main(["--select", ID1, "--out", "secret.json", "--new-range", "bytes=0-67108864"])


def test_strict_requires_matching_complete_206_larger_than_threshold(tmp_path, capsys):
    runtime = private_logs(tmp_path, [request(length=SIZE)],
                           [summary(size=12 * 1024 * 1024)])
    assert extract.main(["--dir", str(runtime)], now=NOW) == 0
    printed = capsys.readouterr().out
    assert "SAFE_CANDIDATES: 0" in printed
    assert "NOT_OVER_24_MIB=1" in printed
    assert "REPLAY_CANDIDATES: 1" in printed


def test_time_remaining_cutoff_and_temporary_credential_caveat(tmp_path, capsys):
    runtime = private_logs(tmp_path, [request(url=signed_url(expires=1000, token=True))])
    assert extract.main(["--dir", str(runtime), "--min-ttl-minutes", "30"], now=NOW) == 0
    printed = capsys.readouterr().out
    assert "EXPIRING_SOON=1" in printed
    assert "REPLAY_CANDIDATES: 0" in printed
    assert "PRIVATE_TOKEN_NEVER_PRINT" not in printed

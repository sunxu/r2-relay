import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "poc" / "real_r2_download_acceptance.py"
SPEC = importlib.util.spec_from_file_location("real_r2_download_acceptance", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def valid_spec():
    return {
        "url": "https://acct.r2.cloudflarestorage.com/bucket/object?X-Amz-Signature=private",
        "headers": [
            ["Range", "bytes=0-3"],
            ["Authorization", "AWS4-HMAC-SHA256 private"],
        ],
        "expected_status_code": 206,
        "expected_size": 4,
        "expected_sha256": "0" * 64,
        "expected_headers": {
            "content-range": "bytes 0-3/4",
            "content-length": "4",
        },
    }


def test_real_r2_spec_loads_only_from_exact_0600_file(tmp_path, monkeypatch):
    path = tmp_path / "private.json"
    path.write_text(json.dumps(valid_spec()), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("R2_RELAY_REAL_GET_SPEC", str(path))

    loaded = MODULE.load_spec()
    assert loaded["expected_status_code"] == 206
    assert loaded["expected_size"] == 4
    assert loaded["headers"][0] == ("Range", "bytes=0-3")
    assert "X-Amz-Signature" in loaded["url"]

    path.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        MODULE.load_spec()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "http://acct.r2.cloudflarestorage.com/bucket/object"),
        ("expected_status_code", 99),
        ("expected_size", -1),
        ("expected_sha256", "not-a-digest"),
        ("expected_headers", {}),
        ("expected_headers", {"content-range": "bad" + chr(10) + "header"}),
        ("headers", [["Authorization", "bad" + chr(10) + "header"]]),
    ],
)
def test_real_r2_spec_rejects_invalid_fields(tmp_path, monkeypatch, field, value):
    data = valid_spec()
    data[field] = value
    path = tmp_path / "private.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("R2_RELAY_REAL_GET_SPEC", str(path))
    with pytest.raises(ValueError):
        MODULE.load_spec()


def test_real_r2_hash_result_supports_memory_and_disk(tmp_path):
    body = b"known-response"
    memory_result = SimpleNamespace(body=body, path=None)
    size, digest = MODULE.hash_result(memory_result)
    assert size == len(body)

    import hashlib
    assert digest == hashlib.sha256(body).hexdigest()

    path = tmp_path / "response.bin"
    path.write_bytes(body)
    disk_result = SimpleNamespace(body=None, path=path)
    assert MODULE.hash_result(disk_result) == (size, digest)



@pytest.mark.parametrize(
    ("protocol", "remote_failures", "disk_used", "memory_used", "leave_temp_file", "leave_body", "expected_pass"),
    [
        ("HTTP/2", 0, 0, 0, False, False, True),
        ("HTTP/1.1", 0, 0, 0, False, False, False),
        ("HTTP/2", 1, 0, 0, False, False, False),
        ("HTTP/2", 0, 1, 0, False, False, False),
        ("HTTP/2", 0, 0, 1, False, False, False),
        ("HTTP/2", 0, 0, 0, True, False, False),
        ("HTTP/2", 0, 0, 0, False, True, False),
    ],
)
def test_real_r2_acceptance_pass_requires_http2_and_complete_cleanup(
    tmp_path, monkeypatch, capsys,
    protocol, remote_failures, disk_used, memory_used, leave_temp_file, leave_body, expected_pass
):
    data = valid_spec()
    body = b"real-r2-acceptance-fixture"
    data["expected_status_code"] = 206
    data["expected_size"] = len(body)
    data["expected_sha256"] = hashlib.sha256(body).hexdigest()
    data["headers"] = [["Range", f"bytes=0-{len(body) - 1}"]]
    data["expected_headers"] = {
        "content-range": f"bytes 0-{len(body) - 1}/{len(body)}",
        "content-length": str(len(body)),
    }
    path = tmp_path / "private.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)

    monkeypatch.setenv("R2_RELAY_REAL_GET_SPEC", str(path))
    monkeypatch.setenv("R2_RELAY_VPS_URL", "https://relay.example:8443")
    monkeypatch.setenv("R2_RELAY_TOKEN", "test-token")

    local_path = tmp_path / "leftover-download.bin"
    body_buffer = bytearray(body)
    if leave_temp_file:
        local_path.write_bytes(body)

    class FakeRelay:
        def __init__(self, endpoint, token):
            self.download_http_versions = [protocol, protocol]
            self.closed = False

        async def close(self):
            self.closed = True

    class FakeCoordinator:
        def __init__(self, relay, settings):
            self.closed = False
            self.remote_cleanup_failures = remote_failures
            self.disk_budget = SimpleNamespace(used=disk_used)
            self.memory_budget = SimpleNamespace(used=memory_used)
            self.relay = relay

        async def download(self, url, headers):
            assert url == data["url"]
            assert headers[0] == ("Range", f"bytes=0-{len(body) - 1}")

            async def cleanup():
                if not leave_temp_file:
                    local_path.unlink(missing_ok=True)
                if not leave_body:
                    body_buffer.clear()

            return SimpleNamespace(
                status_code=206,
                headers=[
                    ("Content-Range", f"bytes 0-{len(body) - 1}/{len(body)}"),
                    ("Content-Length", str(len(body))),
                ],
                body=None if leave_temp_file else body_buffer,
                path=local_path if leave_temp_file else None,
                cleanup=cleanup
            )

        async def close(self):
            self.closed = True

    monkeypatch.setattr(
        MODULE, "load_settings", lambda: SimpleNamespace(download_enabled=True)
    )
    monkeypatch.setattr(MODULE, "RelayClient", FakeRelay)
    monkeypatch.setattr(MODULE, "DownloadCoordinator", FakeCoordinator)

    exit_code = asyncio.run(MODULE.run())
    report = json.loads(capsys.readouterr().out)
    assert exit_code == (0 if expected_pass else 1)
    assert report["status"] == ("PASS_REAL" if expected_pass else "FAIL_REAL")
    assert report["http_status"] == 206
    assert report["mac_to_vps_http_versions"] == [protocol]
    assert report["http2_ok"] is (protocol == "HTTP/2")
    assert report["expected_sha256_match"] is True
    assert report["expected_headers_match"] is True
    assert report["header_checks"] == {
        "content-range": True, "content-length": True
    }
    assert report["vps_task_cleanup_ok"] is (remote_failures == 0)
    assert report["local_temp_cleanup_ok"] is (
        not leave_temp_file and not leave_body and disk_used == 0 and memory_used == 0
    )
    assert report["local_body_released"] is (leave_temp_file or not leave_body)
    assert report["signed_url_or_headers_logged"] is False
    assert local_path.exists() is leave_temp_file



@pytest.mark.parametrize(
    ("expected_headers", "missing_header"),
    [
        ({"content-length": "4"}, "Content-Range"),
        ({"content-range": "bytes 0-3/4"}, "Content-Length"),
    ],
)
def test_real_r2_range_spec_requires_content_range_and_content_length(
    tmp_path, monkeypatch, expected_headers, missing_header
):
    data = valid_spec()
    data["expected_headers"] = expected_headers
    path = tmp_path / "private.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("R2_RELAY_REAL_GET_SPEC", str(path))

    with pytest.raises(ValueError, match=missing_header):
        MODULE.load_spec()


def test_real_r2_non_range_spec_still_requires_at_least_one_expected_header(
    tmp_path, monkeypatch
):
    data = valid_spec()
    data["headers"] = []
    data["expected_headers"] = {"etag": '"object-version"'}
    path = tmp_path / "private.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("R2_RELAY_REAL_GET_SPEC", str(path))

    loaded = MODULE.load_spec()
    assert loaded["expected_headers"] == {"etag": '"object-version"'}


@pytest.mark.asyncio
async def test_real_r2_spec_local_backend_reuses_private_spec_and_stream(tmp_path, monkeypatch, capsys):
    body = b"private-fixture" * 50
    data = valid_spec()
    data.update({
        "expected_status_code": 206,
        "expected_size": len(body),
        "expected_sha256": hashlib.sha256(body).hexdigest(),
        "headers": [["Range", f"bytes=0-{len(body)-1}"]],
        "expected_headers": {
            "content-range": f"bytes 0-{len(body)-1}/{len(body)}",
            "content-length": str(len(body)),
        },
    })
    path = tmp_path / "private.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("R2_RELAY_REAL_GET_SPEC", str(path))
    monkeypatch.delenv("R2_RELAY_VPS_URL", raising=False)
    monkeypatch.delenv("R2_RELAY_TOKEN", raising=False)
    monkeypatch.setattr(
        MODULE, "load_settings",
        lambda: SimpleNamespace(download_enabled=True, download_backend="local"),
    )

    closed = []
    class FakeLocal:
        def __init__(self, settings):
            self.root = tmp_path
            self.disk_budget = SimpleNamespace(used=0)
            self._cleanups = set()
            self.split_gets = 1
            self.child_range_requests = 2

        async def download_stream(self, url, headers):
            assert url == data["url"]
            assert headers == [("Range", f"bytes=0-{len(body)-1}")]
            async def chunks():
                yield body[:123]
                yield body[123:]
            async def cleanup():
                pass
            return SimpleNamespace(
                status_code=206,
                headers=[
                    ("Content-Length", str(len(body))),
                    ("Content-Range", f"bytes 0-{len(body)-1}/{len(body)}"),
                ],
                stream=chunks(), cleanup=cleanup,
            )

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(MODULE, "LocalRangeDownloader", FakeLocal)
    exit_code = await MODULE.run()
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["status"] == "PASS_REAL_LOCAL"
    assert report["child_range_requests"] == 2
    assert report["expected_sha256_match"]
    assert report["local_temp_cleanup_ok"]
    assert report["signed_url_or_headers_logged"] is False
    assert closed == [True]

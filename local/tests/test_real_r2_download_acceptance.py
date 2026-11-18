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
        ("headers", [["Authorization", "bad\\nheader"]]),
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

"""Offline tests for private real-R2 GET through isolated mitmproxy.

All URLs and credentials below are synthetic. Never contacts Cloudflare R2.
"""
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "poc" / "real_r2_mitmproxy_acceptance.py"
SPEC = importlib.util.spec_from_file_location("real_r2_mitmproxy_acceptance", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def example(body: bytes = b"hello") -> dict:
    return {
        "url": "https://example.r2.cloudflarestorage.com/test/object?X-Amz-Signature=private",
        "headers": [["Host", "example.r2.cloudflarestorage.com"], ["Range", "bytes=0-4"]],
        "expected_status_code": 206,
        "expected_size": len(body),
        "expected_sha256": hashlib.sha256(body).hexdigest(),
        "expected_headers": {
            "content-range": "bytes 0-4/5",
            "content-length": str(len(body)),
            "etag": '"test-etag"',
        },
    }


def test_private_proxy_spec_requires_mode_0600(tmp_path, monkeypatch):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(example()), encoding="utf-8")
    spec.chmod(0o600)
    monkeypatch.setenv("R2_RELAY_REAL_GET_SPEC", str(spec))
    assert MODULE.load_private_spec()["expected_size"] == 5
    spec.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        MODULE.load_private_spec()


@pytest.mark.parametrize("mutator", [
    lambda obj: obj.update(url="http://example.r2.cloudflarestorage.com/file"),
    lambda obj: obj.update(expected_size=0),
    lambda obj: obj.update(expected_sha256="not-a-sha"),
    lambda obj: obj.update(expected_headers={"etag": '"test-etag"'}),
    lambda obj: obj.update(headers=[["Range", "ok\nunsafe"]]),
])
def test_private_proxy_spec_rejects_invalid_data(tmp_path, monkeypatch, mutator):
    obj = example()
    mutator(obj)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(obj), encoding="utf-8")
    spec.chmod(0o600)
    monkeypatch.setenv("R2_RELAY_REAL_GET_SPEC", str(spec))
    with pytest.raises(ValueError):
        MODULE.load_private_spec()


@pytest.mark.parametrize(
    ("status", "body", "etag", "passed"),
    [
        (206, b"hello", '"test-etag"', True),
        (200, b"hello", '"test-etag"', False),
        (206, b"wrong", '"test-etag"', False),
        (206, b"hello", '"changed"', False),
    ],
)
def test_proxy_acceptance_requires_status_size_digest_and_headers(status, body, etag, passed):
    result = MODULE.check_response(
        example(), status=status, headers=httpx.Headers({
            "Content-Range": "bytes 0-4/5",
            "Content-Length": "5",
            "ETag": etag,
        }), size=len(body), digest=hashlib.sha256(body).hexdigest()
    )
    assert result["status"] == ("PASS_REAL_PROXY" if passed else "FAIL_REAL_PROXY")
    assert result["signed_url_or_headers_logged"] is False


def test_proxy_acceptance_streams_without_logging_signed_url(tmp_path, monkeypatch, capsys):
    body = b"hello"
    obj = example(body)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(obj), encoding="utf-8")
    spec.chmod(0o600)
    ca = tmp_path / "ca.pem"
    ca.write_text("dummy-ca", encoding="utf-8")
    monkeypatch.setenv("R2_RELAY_REAL_GET_SPEC", str(spec))
    monkeypatch.setenv("R2_RELAY_MITMPROXY_CA", str(ca))

    class FakeResponse:
        status_code = 206
        headers = httpx.Headers({
            "Content-Range": "bytes 0-4/5",
            "Content-Length": "5",
            "ETag": '"test-etag"',
        })
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def iter_bytes(self, chunk_size):
            yield body[:2]
            yield body[2:]

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["proxy"] == "http://127.0.0.1:18586"
            assert kwargs["trust_env"] is False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def stream(self, method, url, headers):
            assert method == "GET"
            assert url == obj["url"]
            assert headers == obj["headers"]
            return FakeResponse()

    monkeypatch.setattr(MODULE.httpx, "Client", FakeClient)
    assert MODULE.run() == 0
    out = capsys.readouterr().out
    assert json.loads(out)["status"] == "PASS_REAL_PROXY"
    assert "X-Amz-Signature" not in out
    assert "private" not in out

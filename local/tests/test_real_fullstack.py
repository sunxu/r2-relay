"""Actual aiohttp→mitmdump addon→HTTPS VPS→mock R2 across processes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys

import aiohttp
import pytest


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for_port(port: int, process: subprocess.Popen, log: Path) -> None:
    for _ in range(120):
        if process.poll() is not None:
            pytest.fail(f"service stopped: {log.read_text()[-4000:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            await asyncio.sleep(0.1)
    pytest.fail(f"service startup timeout: {log.read_text()[-4000:]}")


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=6)


@pytest.mark.asyncio
async def test_real_connect_to_real_https_vps_and_mock_r2(tmp_path):
    assert shutil.which("mitmdump"), "mitmdump executable required"
    cert, key = tmp_path / "vps.crt", tmp_path / "vps.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "1",
         "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    vps_port, proxy_port = free_port(), free_port()
    vps_log, proxy_log = tmp_path / "vps.log", tmp_path / "proxy.log"
    calls, cache = tmp_path / "r2-calls.jsonl", tmp_path / "cache"
    proxy_conf = tmp_path / "proxy-conf"
    root = Path(__file__).resolve().parents[2]
    repo_root = root.parent
    entry = repo_root / "local" / "addon_entry.py"
    fixture = Path(__file__).parent / "fixtures" / "mock_vps_entry.py"
    # The VPS entrypoint can import installed Python packages without PYTHONPATH tricks.
    vps_env = dict(
        os.environ, R2_TEST_CALL_LOG=str(calls), R2_TEST_CACHE_DIR=str(cache),
        R2_RELAY_TOKEN="integration-token",
    )
    vps_cmd = [
        sys.executable, "-m", "uvicorn", "mock_vps_entry:app",
        "--app-dir", str(fixture.parent), "--host", "127.0.0.1",
        "--port", str(vps_port), "--ssl-certfile", str(cert),
        "--ssl-keyfile", str(key), "--log-level", "warning",
    ]
    proxy_env = dict(
        os.environ,
        R2_RELAY_VPS_URL=f"https://localhost:{vps_port}",
        R2_RELAY_VPS_CA_FILE=str(cert),
        R2_RELAY_TOKEN="integration-token",
    )
    proxy_cmd = [
        shutil.which("mitmdump"), "--mode", "regular",
        "--listen-host", "127.0.0.1", "--listen-port", str(proxy_port),
        "--set", f"confdir={proxy_conf}", "--set", "upstream_cert=false",
        "--allow-hosts", r".*\.r2\.cloudflarestorage\.com(:443)?$",
        "-s", str(entry),
    ]
    with vps_log.open("w") as vo, proxy_log.open("w") as po:
        vps_process = subprocess.Popen(vps_cmd, stdout=vo, stderr=subprocess.STDOUT, env=vps_env)
        proxy_process = None
        try:
            await wait_for_port(vps_port, vps_process, vps_log)
            proxy_process = subprocess.Popen(
                proxy_cmd, stdout=po, stderr=subprocess.STDOUT, env=proxy_env,
            )
            await wait_for_port(proxy_port, proxy_process, proxy_log)
            ca = proxy_conf / "mitmproxy-ca-cert.pem"
            for _ in range(50):
                if ca.is_file():
                    break
                await asyncio.sleep(0.05)
            assert ca.is_file()
            context = ssl.create_default_context()
            context.load_verify_locations(cafile=str(ca))
            body = b"r2-relay full-stack test bytes" * 350000  # approx 10 MiB
            expected = hashlib.md5(body, usedforsecurity=False).hexdigest()
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as client:
                for upload_id in ["first", "second"]:
                    url = (
                        "https://acct.r2.cloudflarestorage.com/bucket/part.bin?"
                        f"uploadId={upload_id}&partNumber=10&X-Amz-SignedHeaders=host"
                    )
                    async with client.put(
                        url, data=body, proxy=f"http://127.0.0.1:{proxy_port}",
                        ssl=context,
                    ) as result:
                        assert result.status == 200
                        assert result.headers["ETag"] == f'"{expected}"'
                        assert await result.read() == b""
            lines = [json.loads(line) for line in calls.read_text().splitlines()]
            assert len(lines) == 3, lines
            assert [x["attempt"] for x in lines] == [1, 2, 1]
            assert {x["body_md5"] for x in lines} == {expected}
            assert {x["content_length"] for x in lines} == {str(len(body))}
            assert {x["host"] for x in lines} == {"acct.r2.cloudflarestorage.com"}
            assert len(list(cache.glob("*/*.part"))) == 1, "second request must reuse VPS bytes"
        finally:
            if proxy_process is not None:
                stop(proxy_process)
            stop(vps_process)

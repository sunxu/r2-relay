"""GitHub Runner full network path, without a VPS or Cloudflare account."""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
from urllib.parse import urlencode

import aiohttp
import httpx
import pytest


def port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def ready(which, proc, log):
    for _ in range(160):
        if proc.poll() is not None:
            pytest.fail(f"{which} exited: {log.read_text()[-3000:]}")
        try:
            with socket.create_connection(("127.0.0.1", which), timeout=0.15):
                return
        except OSError:
            await asyncio.sleep(0.1)
    pytest.fail(f"service {which} timed out: {log.read_text()[-3000:]}")


def stop(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=8)


@pytest.mark.asyncio
async def test_caddy_https_to_real_vps_to_real_mock_r2(tmp_path):
    if os.environ.get("R2_RELAY_RUN_RUNNER_FULLSTACK") != "1":
        pytest.skip("opt-in full-stack Runner test")
    assert shutil.which("docker") and shutil.which("mitmdump")
    root = Path(__file__).resolve().parents[2]
    fixture_dir = Path(__file__).resolve().parent
    mock_port, vps_port, proxy_port = port(), port(), port()
    # Port 8443 is tested on the Runner; Caddy proxy listens only to loopback.
    # The host-network container uses an explicit test certificate.
    caddy_port = 8443
    calls = tmp_path / "mock_calls.jsonl"
    get_calls = tmp_path / "mock_get_calls.jsonl"
    download_config = tmp_path / "download.toml"
    download_config.write_text(
        "[download]\\n"
        "enabled = true\\n"
        "max_concurrency = 16\\n"
        "global_max_concurrency = 32\\n"
        "small_response_memory_mib = 32\\n"
        "max_temp_mib = 128\\n"
        f'temp_dir = "{tmp_path / "mac-downloads"}"\\n',
        encoding="utf-8",
    )
    token = "runner-integration-token"
    hmac_key = "runner-independent-test-key"
    cert, key = tmp_path / "runner.crt", tmp_path / "runner.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-out", str(cert), "-keyout", str(key), "-days", "1",
         "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(
        "{\n auto_https disable_redirects\n}\n"
        f"https://localhost:{caddy_port} {{\n"
        " tls /config/runner.crt /config/runner.key\n"
        f" reverse_proxy 127.0.0.1:{vps_port}\n"
        "}\n"
    )
    # Config bundle is bind-mounted into Caddy as read-only.
    logfiles = {name: tmp_path / f"{name}.log"
                for name in ("mock", "vps", "caddy", "proxy")}
    mock_env = dict(os.environ, R2_TEST_HMAC_KEY=hmac_key,
                    R2_TEST_CALL_LOG=str(calls), R2_TEST_GET_LOG=str(get_calls))
    vps_env = dict(os.environ, R2_TEST_CACHE_DIR=str(tmp_path / "cache"),
                   R2_TEST_DOWNLOAD_CACHE_DIR=str(tmp_path / "download-cache"),
                   R2_TEST_MOCK_PORT=str(mock_port), R2_RELAY_TOKEN=token)
    proxy_env = dict(os.environ, R2_RELAY_VPS_URL=f"https://localhost:{caddy_port}",
                     R2_RELAY_VPS_CA_FILE=str(cert), R2_RELAY_TOKEN=token,
                     R2_RELAY_CONFIG=str(download_config), R2_RELAY_HTTP2="1")
    cmds = [
        ("mock", [sys.executable, "-m", "uvicorn", "mock_r2:app", "--app-dir",
                  str(fixture_dir), "--host", "127.0.0.1", "--port", str(mock_port)]),
        ("vps", [sys.executable, "-m", "uvicorn", "mock_vps:app", "--app-dir",
                 str(fixture_dir), "--host", "127.0.0.1", "--port", str(vps_port)]),
        ("caddy", ["docker", "run", "--rm", "--network", "host",
                   "-v", f"{tmp_path}:/config:ro", "caddy:2",
                   "caddy", "run", "--config", "/config/Caddyfile"]),
        ("proxy", ["mitmdump", "--mode", "regular", "--listen-host", "127.0.0.1",
                   "--listen-port", str(proxy_port), "--set",
                   f"confdir={tmp_path / 'mitmconf'}", "--set", "upstream_cert=false",
                   "--allow-hosts", r"(.*\.r2\.cloudflarestorage\.com(:443)?|127\.0\.0\.1(:[0-9]+)?)$",
                   "-s", str(root / "local" / "addon_entry.py")]),
    ]
    envs = [mock_env, vps_env, dict(os.environ), proxy_env]
    ports = [mock_port, vps_port, caddy_port, proxy_port]
    procs, outputs = [], []
    try:
        for (name, cmd), env, bind_port in zip(cmds, envs, ports):
            output = logfiles[name].open("w")
            outputs.append(output)
            process = subprocess.Popen(cmd, cwd=root, env=env, stdout=output,
                                       stderr=subprocess.STDOUT)
            procs.append(process)
            await ready(bind_port, process, logfiles[name])
        ca = tmp_path / "mitmconf" / "mitmproxy-ca-cert.pem"
        for _ in range(60):
            if ca.exists():
                break
            await asyncio.sleep(0.1)
        assert ca.exists()
        trust = ssl.create_default_context()
        trust.load_verify_locations(cafile=str(ca))
        content = b"runner full network stack" * 400_000
        digest = hashlib.md5(content, usedforsecurity=False).hexdigest()
        statuses = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as client:
            for upload_id, expected_status in [
                ("first", 200), ("second", 200), ("fail502", 200),
                ("fail429", 200), ("forbidden", 403),
            ]:
                witness = hmac.new(
                    hmac_key.encode(),
                    f"{upload_id}|7|{digest}".encode(), "sha256"
                ).hexdigest()
                url = ("https://acct.r2.cloudflarestorage.com/bucket/object?"
                       + urlencode({"uploadId": upload_id, "partNumber": "7",
                                    "X-Test-Witness": witness}))
                async with client.put(url, data=content, ssl=trust,
                                      proxy=f"http://127.0.0.1:{proxy_port}") as response:
                    statuses.append(response.status)
                    assert response.status == expected_status
                    if expected_status == 200:
                        assert response.headers["ETag"] == f'"{digest}"'
                    await response.read()
        # New GET path: the real Mac addon and RelayClient traverse mitmproxy,
        # Caddy TLS/HTTP2, the real VPS DownloadManager, and a streaming Mock R2.
        total = 25 * 1024 * 1024
        range_header = f"bytes=0-{total - 1}"
        get_url = "https://acct.r2.cloudflarestorage.com/bucket/object?version=download-test"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as client:
            async with client.get(
                get_url,
                headers={
                    "Range": range_header,
                    "Authorization": "AWS4-HMAC-SHA256 signed-get-test",
                    "X-Test-Signed-Marker": "signed-get-marker",
                },
                ssl=trust,
                proxy=f"http://127.0.0.1:{proxy_port}",
            ) as response:
                assert response.status == 206
                assert response.headers["Content-Range"] == f"bytes 0-{total - 1}/{total}"
                assert response.headers["Content-Length"] == str(total)
                assert response.headers["ETag"] == '"mock-get-v1"'
                assert response.headers["X-Mock-Signed-Marker"] == "signed-get-marker"
                downloaded = await response.read()
        expected = bytes((index % 251 for index in range(total)))
        assert len(downloaded) == total
        assert hashlib.sha256(downloaded).digest() == hashlib.sha256(expected).digest()
        get_rows = [json.loads(x) for x in get_calls.read_text().splitlines()]
        assert len(get_rows) == 1
        assert get_rows[0]["original_url"] == get_url
        assert get_rows[0]["original_host"] == "acct.r2.cloudflarestorage.com"
        assert get_rows[0]["range"] == range_header
        assert get_rows[0]["signed_marker"] == "signed-get-marker"
        assert get_rows[0]["authorization_present"] is True
        outputs[3].flush()
        proxy_log = logfiles["proxy"].read_text()
        assert "download_relay_http_version=HTTP/2" in proxy_log

        # Hit/miss accounting goes through real Caddy TLS and HTTP/2.
        async with httpx.AsyncClient(http2=True, verify=str(cert), trust_env=False) as http:
            response = await http.get(
                f"https://localhost:{caddy_port}/v1/metrics",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            assert response.http_version == "HTTP/2"
            metrics = response.json()
            assert metrics["prepare_miss"] == 1
            assert metrics["prepare_hit"] == 4
            assert metrics["staged_bytes"] == len(content)
            assert metrics["r2_attempts"] == 7

        # Cold VPS restart retains only disk cache, not in-flight task state.
        # Test restoration by restarting the actual Uvicorn process.
        stop(procs[1])
        procs[1] = subprocess.Popen(
            cmds[1][1], cwd=root, env=vps_env, stdout=outputs[1],
            stderr=subprocess.STDOUT,
        )
        await ready(vps_port, procs[1], logfiles["vps"])
        witness = hmac.new(
            hmac_key.encode(),
            f"afterrestart|7|{digest}".encode(), "sha256"
        ).hexdigest()
        url = ("https://acct.r2.cloudflarestorage.com/bucket/object?"
               + urlencode({"uploadId": "afterrestart", "partNumber": "7",
                            "X-Test-Witness": witness}))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as client:
            async with client.put(
                url, data=content, ssl=trust,
                proxy=f"http://127.0.0.1:{proxy_port}",
            ) as response:
                assert response.status == 200
                assert response.headers["ETag"] == f'"{digest}"'
                await response.read()
        async with httpx.AsyncClient(http2=True, verify=str(cert), trust_env=False) as http:
            restored = (await http.get(
                f"https://localhost:{caddy_port}/v1/metrics",
                headers={"Authorization": f"Bearer {token}"},
            )).json()
            assert restored["prepare_hit"] == 1
            assert restored["prepare_miss"] == 0

        rows = [json.loads(x) for x in calls.read_text().splitlines()]
        assert [r["upload_id"] for r in rows] == [
            "first", "second", "fail502", "fail502", "fail429",
            "fail429", "forbidden", "afterrestart"
        ]
        assert len(list((tmp_path / "cache").glob("*/*.part"))) == 1
        assert all(r["body_md5"] == digest for r in rows)
        result = {
            "schema": "r2_relay_runner_fullstack_safe_v1",
            "status": "RUNNER_FULLSTACK_PASS",
            "git_sha": os.environ.get("GITHUB_SHA", "LOCAL_TEST"),
            "components": ["aiohttp", "mitmproxy", "caddy_tls_8443",
                           "vps_asgi_tcp", "mock_r2_tcp"],
            "simulated_put_count": len(rows),
            "simulated_get_count": len(get_rows),
            "simulated_get_bytes": len(downloaded),
            "mac_to_vps_download_http2_verified": True,
            "vps_restart_cache_recovered": True,
            "caddy_http2_verified": True,
            "cache_miss_before_restart": metrics["prepare_miss"],
            "cache_hit_before_restart": metrics["prepare_hit"],
            "statuses": statuses,
            "r2_real": "NOT_TESTED",
            "vps_public_network": "NOT_TESTED",
            "cloudflare_sigv4": "NOT_TESTED",
            "network_bandwidth": "NOT_TESTED",
        }
        Path(os.environ["R2_RELAY_RUNNER_REPORT"]).write_text(
            json.dumps(result, sort_keys=True, indent=2) + "\n")
    except Exception:
        for name, logfile in logfiles.items():
            if logfile.exists():
                print(f"\n===== {name.upper()} LOG =====\n{logfile.read_text()[-12000:]}")
        raise
    finally:
        for proc in reversed(procs):
            stop(proc)
        for output in outputs:
            output.close()

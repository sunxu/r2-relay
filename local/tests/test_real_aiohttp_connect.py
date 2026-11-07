"""Real aiohttp + real mitmdump CONNECT/TLS test; no external network or R2 credentials."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import time

import aiohttp
from aiohttp import web
import pytest


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_aiohttp_connect_full_request_and_ca(tmp_path):
    mitmdump = shutil.which("mitmdump")
    if not mitmdump:
        pytest.fail("mitmdump is required for real HTTPS CONNECT smoke test")
    port = unused_port()
    confdir = tmp_path / "mitm-conf"
    capture_file = tmp_path / "capture.json"
    script = Path(__file__).parent / "fixtures" / "mitm_capture.py"
    log = tmp_path / "mitmdump.log"
    env = dict(os.environ, MITM_CAPTURE_RESULT=str(capture_file))
    cmd = [
        mitmdump, "--mode", "regular", "--listen-host", "127.0.0.1",
        "--listen-port", str(port), "--set", f"confdir={confdir}",
        "--set", "upstream_cert=false",
        "--allow-hosts", r".*\.r2\.cloudflarestorage\.com(:443)?$",
        "-s", str(script),
    ]
    with log.open("w") as output:
        process = subprocess.Popen(cmd, stdout=output, stderr=subprocess.STDOUT, env=env)
        try:
            ca_file = confdir / "mitmproxy-ca-cert.pem"
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail("mitmdump exited:\n" + log.read_text()[-5000:])
                if ca_file.exists():
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        pass
                await asyncio.sleep(0.1)
            else:
                pytest.fail("mitmdump failed to start:\n" + log.read_text()[-5000:])

            # Preserve the normal trust store and add only the mitmproxy CA.
            context = ssl.create_default_context()
            context.load_verify_locations(cafile=str(ca_file))
            body = b"real aiohttp CONNECT payload" * 200
            url = ("https://abc.r2.cloudflarestorage.com/bucket/key?"
                   "uploadId=test-123&partNumber=3")
            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.put(
                    url, data=body, ssl=context,
                    proxy=f"http://127.0.0.1:{port}",
                ) as response:
                    assert response.status == 200
                    assert response.headers["ETag"] == '"mitm-real-connect-proof"'
                    assert response.headers["X-Captured-Md5"] == hashlib.md5(
                        body, usedforsecurity=False
                    ).hexdigest()
                    assert await response.read() == b""
            data = json.loads(capture_file.read_text())
            assert data["method"] == "PUT"
            assert data["length"] == len(body)
            assert data["md5"] == hashlib.md5(body, usedforsecurity=False).hexdigest()
            assert "uploadId=test-123" in data["url"]

            # Non-R2 HTTPS must remain an original CONNECT tunnel, not hit the
            # R2 addon. Trust a separate self-signed localhost cert explicitly.
            cert = tmp_path / "localhost.crt"
            key = tmp_path / "localhost.key"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(cert), "-days", "1",
                 "-subj", "/CN=localhost",
                 "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ssl.load_cert_chain(str(cert), str(key))
            app = web.Application()

            async def non_r2(request):
                received = await request.read()
                return web.Response(body=b"direct-tunnel:" + received)

            app.router.add_put("/other", non_r2)
            runner = web.AppRunner(app)
            await runner.setup()
            server_port = unused_port()
            site = web.TCPSite(runner, "127.0.0.1", server_port, ssl_context=server_ssl)
            await site.start()
            try:
                combined_context = ssl.create_default_context()
                combined_context.load_verify_locations(cafile=str(ca_file))
                combined_context.load_verify_locations(cafile=str(cert))
                async with aiohttp.ClientSession(timeout=timeout) as client:
                    async with client.put(
                        f"https://localhost:{server_port}/other",
                        data=b"untouched", ssl=combined_context,
                        proxy=f"http://127.0.0.1:{port}",
                    ) as response:
                        assert response.status == 200
                        assert await response.read() == b"direct-tunnel:untouched"
                # Non-R2 never reached the capture addon.
                assert json.loads(capture_file.read_text())["url"] == data["url"]
            finally:
                await runner.cleanup()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

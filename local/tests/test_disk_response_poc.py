from __future__ import annotations

import asyncio
import os
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path

import httpx
import pytest

try:
    import psutil
except ImportError:  # pragma: no cover - mitmproxy normally installs psutil
    psutil = None


LOCAL_ROOT = Path(__file__).resolve().parents[1]
ADDON = LOCAL_ROOT / "poc" / "disk_response_addon.py"
MIB = 1024 * 1024


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def rss_bytes(pid: int) -> int:
    if psutil is not None:
        try:
            return int(psutil.Process(pid).memory_info().rss)
        except psutil.Error:
            return 0
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return 0


async def serve_file(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    path: Path,
    disconnected: asyncio.Event | None = None,
) -> None:
    try:
        await reader.readuntil(b"\r\n\r\n")
        size = path.stat().st_size
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            + f"Content-Length: {size}\r\n".encode()
            + b"Content-Type: application/octet-stream\r\n"
            + b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        with path.open("rb", buffering=0) as stream:
            while True:
                chunk = await asyncio.to_thread(stream.read, MIB)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
    except (BrokenPipeError, ConnectionResetError, asyncio.IncompleteReadError):
        if disconnected is not None:
            disconnected.set()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def wait_for_proxy(process: subprocess.Popen, port: int, cert: Path, log_path: Path) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"mitmdump exited early; log: {log_path.read_text(errors='replace')}")
        if cert.is_file():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                pass
        await asyncio.sleep(0.1)
    raise TimeoutError(f"mitmdump did not become ready; log: {log_path.read_text(errors='replace')}")


@pytest.mark.asyncio
async def test_disk_backed_response_streams_through_mitmproxy_http11(tmp_path: Path) -> None:
    mitmdump = shutil.which("mitmdump")
    if not mitmdump:
        pytest.skip("mitmdump executable is not installed")

    size_mib = int(os.environ.get("R2_POC_SIZE_MIB", "64"))
    assert size_mib >= 32
    expected_size = size_mib * MIB
    source_file = tmp_path / "sparse-1g.bin"
    with source_file.open("wb") as stream:
        stream.truncate(expected_size)

    origin_port = free_port()
    proxy_port = free_port()
    disconnected = asyncio.Event()
    origin = await asyncio.start_server(
        lambda reader, writer: serve_file(reader, writer, source_file, disconnected),
        "127.0.0.1",
        origin_port,
    )

    confdir = tmp_path / "mitm-conf"
    confdir.mkdir()
    cert = confdir / "mitmproxy-ca-cert.pem"
    addon = ADDON
    log_path = tmp_path / "mitmdump.log"
    env = os.environ.copy()
    env["R2_POC_ORIGIN_PORT"] = str(origin_port)
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                mitmdump,
                "--quiet",
                "--mode", "regular",
                "--listen-host", "127.0.0.1",
                "--listen-port", str(proxy_port),
                "--set", f"confdir={confdir}",
                "--set", "connection_strategy=lazy",
                "-s", str(addon),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=log,
        )

    try:
        await wait_for_proxy(process, proxy_port, cert, log_path)
        baseline_rss = rss_bytes(process.pid)
        peak_rss = baseline_rss
        stop_monitor = asyncio.Event()

        async def monitor_rss() -> None:
            nonlocal peak_rss
            while not stop_monitor.is_set():
                peak_rss = max(peak_rss, rss_bytes(process.pid))
                await asyncio.sleep(0.025)

        monitor = asyncio.create_task(monitor_rss())
        try:
            async with httpx.AsyncClient(
                proxy=f"http://127.0.0.1:{proxy_port}",
                verify=ssl.create_default_context(cafile=str(cert)),
                http2=False,
                trust_env=False,
                timeout=httpx.Timeout(180.0, connect=15.0),
            ) as client:
                async with client.stream("GET", "https://r2-poc.example/file") as response:
                    assert response.status_code == 200
                    assert int(response.headers["content-length"]) == expected_size
                    assert response.http_version == "HTTP/1.1"
                    received = 0
                    async for chunk in response.aiter_raw():
                        assert not any(chunk), "sparse source file should contain only zero bytes"
                        received += len(chunk)
                    assert received == expected_size

                # A second request is intentionally abandoned after its first body chunk.
                async with client.stream("GET", "https://r2-poc.example/file") as response:
                    assert response.status_code == 200
                    async for chunk in response.aiter_raw():
                        assert chunk
                        break
                await asyncio.wait_for(disconnected.wait(), timeout=15)
        finally:
            stop_monitor.set()
            await monitor

        peak_delta_mib = max(0, peak_rss - baseline_rss) / MIB
        print(f"disk response PoC: size_mib={size_mib}, http=1.1, mitmdump_rss_delta_mib={peak_delta_mib:.1f}")
        assert peak_delta_mib < 128, (
            f"mitmdump RSS grew by {peak_delta_mib:.1f} MiB while relaying "
            f"{size_mib} MiB; baseline={baseline_rss / MIB:.1f} MiB, "
            f"peak={peak_rss / MIB:.1f} MiB"
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        origin.close()
        await origin.wait_closed()

"""Offline workload benchmark. All R2 requests use MockTransport; no external network.

Run after: python -m pip install -e './local[test]' -e './vps[test]'
Example: python benchmarks/offline_benchmark.py --parts 100 --part-mib 10 --parallel 8
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

import httpx

from r2relay_local.relay import RelayClient
from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore


class MeteredCache(CacheStore):
    def __init__(self, root: Path):
        super().__init__(root)
        self.received_body_bytes = 0

    async def put(self, md5, chunks, expected_size):
        async def count():
            async for chunk in chunks:
                self.received_body_bytes += len(chunk)
                yield chunk
        return await super().put(md5, count(), expected_size)


async def run(parts: int, part_size: int, parallel: int, root: Path) -> dict:
    cache = MeteredCache(root)
    r2_puts = 0

    def mock_r2(request: httpx.Request):
        nonlocal r2_puts
        r2_puts += 1
        return httpx.Response(200, headers={"ETag": '"mock-r2-response"'})

    app = create_app(
        cache, token="offline-test",
        r2_transport=httpx.MockTransport(mock_r2),
        retry_delays=(0, 0, 0),
    )
    limiter = asyncio.Semaphore(parallel)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as connection:
        client = RelayClient(
            "https://offline.invalid:8443", "offline-test",
            client=connection, poll_interval=0.001, execution_timeout=120,
        )

        async def batch(round_id: int):
            async def upload_one(index: int):
                async with limiter:
                    # The prefix makes each part distinct without expensive randomness.
                    payload = (index.to_bytes(8, "little")
                               + bytes(part_size - 8))
                    url = (
                        "https://bench.r2.cloudflarestorage.com/bucket/key?"
                        f"uploadId=round{round_id}&partNumber={index + 1}"
                    )
                    result = await client.upload(url, [], payload)
                    if result.status != 200:
                        raise RuntimeError(f"unexpected mock R2 status: {result.status}")

            start = time.perf_counter()
            await asyncio.gather(*(upload_one(i) for i in range(parts)))
            return time.perf_counter() - start

        first_seconds = await batch(1)
        first_bytes = cache.received_body_bytes
        second_seconds = await batch(2)
        second_bytes = cache.received_body_bytes - first_bytes

    return {
        "kind": "OFFLINE_SIMULATION_NOT_REAL_NETWORK",
        "parts": parts,
        "part_bytes": part_size,
        "parallel": parallel,
        "first_seconds": round(first_seconds, 3),
        "repeat_seconds": round(second_seconds, 3),
        "first_vps_body_bytes": first_bytes,
        "repeat_vps_body_bytes": second_bytes,
        "cache_files": len(list(root.glob("*/*.part"))),
        "mock_r2_puts": r2_puts,
        "real_bandwidth_tested": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parts", type=int, default=16)
    parser.add_argument("--part-mib", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=8)
    args = parser.parse_args()
    if not (1 <= args.parts <= 200 and 1 <= args.part_mib <= 32 and
            1 <= args.parallel <= 32):
        parser.error("parts=1..200, part-mib=1..32, parallel=1..32")
    with tempfile.TemporaryDirectory(prefix="r2-relay-bench-") as directory:
        report = asyncio.run(run(
            args.parts, args.part_mib * 1024 * 1024, args.parallel,
            Path(directory),
        ))
        print(json.dumps(report, indent=2))
        assert report["first_vps_body_bytes"] == args.parts * args.part_mib * 1024 * 1024
        assert report["repeat_vps_body_bytes"] == 0
        assert report["cache_files"] == args.parts
        assert report["mock_r2_puts"] == 2 * args.parts


if __name__ == "__main__":
    main()

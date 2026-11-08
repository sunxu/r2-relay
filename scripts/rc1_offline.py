"""Run the offline RC1 quality gate. Never contacts a real R2 account."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(args: list[str]) -> None:
    result = subprocess.run(args, cwd=ROOT, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="8x1MiB smoke benchmark")
    args = parser.parse_args(argv)

    print("RC1 offline gate: no real VPS or R2 access", flush=True)
    run([sys.executable, "-m", "pytest", "-q",
         "vps/tests", "local/tests", "benchmarks/test_offline_benchmark.py"])
    run([sys.executable, "benchmarks/offline_benchmark.py",
         "--parts", "8" if args.quick else "100",
         "--part-mib", "1" if args.quick else "10",
         "--parallel", "4" if args.quick else "8"])
    print(json.dumps({
        "status": "RC1_OFFLINE_GATE_PASS",
        "real_vps_used": False,
        "real_r2_used": False,
        "real_network_performance_verified": False,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

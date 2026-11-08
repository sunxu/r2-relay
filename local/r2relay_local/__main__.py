"""Python entry point for the local explicit HTTP(S) proxy."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def command(listen: str, port: int, addon_entry: Path) -> list[str]:
    executable = shutil.which("mitmdump")
    if not executable:
        raise RuntimeError("mitmdump executable not found; install r2relay-local")
    if not (1 <= port <= 65535):
        raise ValueError("invalid listen port")
    return [
        executable, "--mode", "regular",
        "--listen-host", listen, "--listen-port", str(port),
        "--allow-hosts", r".*\.r2\.cloudflarestorage\.com(:443)?$",
        "--set", "ssl_insecure=false",
        "-s", str(addon_entry),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R2 Relay local Python proxy")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", default=18585, type=int)
    args = parser.parse_args(argv)
    if not os.environ.get("R2_RELAY_TOKEN"):
        parser.error("R2_RELAY_TOKEN environment variable is required")
    path = Path(__file__).resolve().parent.parent / "addon_entry.py"
    return subprocess.call(command(args.listen_host, args.listen_port, path))


if __name__ == "__main__":
    sys.exit(main())

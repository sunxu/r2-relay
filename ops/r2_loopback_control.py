#!/usr/bin/env python3
"""Root-side fixed service verification/start. Never outputs production secrets."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import time
from urllib.request import build_opener, ProxyHandler

UNIT = Path("/etc/systemd/system/r2-relay-vps.service")
SOURCE = Path("/opt/r2-relay/deploy/vps.service")
ENV_DIR = Path("/etc/r2-relay")
ENV_FILE = ENV_DIR / "vps.env"
CACHE = Path("/var/lib/r2-relay/cache")
UVICORN = Path("/opt/r2-relay/.venv/bin/uvicorn")
SERVICE = "r2-relay-vps.service"
BOUNDS = {
    "R2_RELAY_MAX_EXECUTIONS": (1, 100000),
    "R2_RELAY_MAX_CONCURRENT": (1, 1024),
    "R2_RELAY_EXECUTION_TTL": (1, 86400),
    "R2_RELAY_MAX_PART_BYTES": (1, 33554432),
    "R2_RELAY_MIN_FREE_BYTES": (1, 2**50),
    "R2_RELAY_MAX_RETAINED_RESULT_BYTES": (1, 2**40),
}

def command(args, timeout=20):
    try:
        p = subprocess.run(args, shell=False, check=False,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=timeout,
                           env={"PATH": "/usr/bin:/bin", "LANG": "C"})
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("fixed_command_unavailable") from None
    if p.returncode:
        raise ValueError("fixed_command_failed")
    return p.stdout.strip()

def regular(path, mode=None):
    try:
        st = path.lstat()
    except OSError:
        raise ValueError("required_file_missing") from None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0:
        raise ValueError("unsafe_file_ownership")
    if mode is not None and stat.S_IMODE(st.st_mode) != mode:
        raise ValueError("unsafe_file_mode")

def parse_private_env():
    regular(ENV_FILE, 0o600)
    values = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("env_syntax")
        key, val = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise ValueError("env_key_invalid")
        values[key] = val.strip()
    required = set(BOUNDS) | {"R2_RELAY_TOKEN", "R2_RELAY_CACHE_DIR"}
    if not required.issubset(values):
        raise ValueError("env_keys_missing")
    token = values["R2_RELAY_TOKEN"]
    if (len(token) < 48 or not re.fullmatch(r"[A-Za-z0-9_-]+", token)
            or token.startswith(("REPLACE_", "CHANGEME", "YOUR_"))):
        raise ValueError("token_invalid")
    if values["R2_RELAY_CACHE_DIR"] != str(CACHE):
        raise ValueError("cache_path_mismatch")
    for key, (lo, hi) in BOUNDS.items():
        value = values[key]
        if not value.isdecimal() or not lo <= int(value) <= hi:
            raise ValueError("env_bound_invalid")
    return len(required)

def bound_addresses(port):
    """Inspect all network interfaces, not only localhost."""
    lines = command(["/usr/bin/ss", "-H", "-lnt", "sport", "=", f":{port}"])
    addresses = []
    for line in lines.splitlines():
        parts = line.split()
        if len(parts) < 5:
            raise ValueError("listener_output_invalid")
        addresses.append(parts[3])
    return addresses

def listening(port):
    return bool(bound_addresses(port))

def systemd_state():
    if command(["/usr/bin/systemctl", "show", SERVICE, "-p", "FragmentPath", "--value"]) != str(UNIT):
        raise ValueError("service_unit_path_invalid")
    if command(["/usr/bin/systemctl", "show", SERVICE, "-p", "DropInPaths", "--value"]):
        raise ValueError("service_dropins_present")
    state = command(["/usr/bin/systemctl", "show", SERVICE, "-p", "ActiveState", "--value"])
    if state not in ("active", "inactive"):
        raise ValueError("service_state_unexpected")
    return state

def verify():
    if os.geteuid() != 0:
        raise ValueError("root_required")
    regular(UNIT, 0o644)
    regular(SOURCE)
    if UNIT.read_bytes() != SOURCE.read_bytes():
        raise ValueError("unit_template_mismatch")
    try:
        d = ENV_DIR.lstat()
        c = CACHE.lstat()
    except OSError:
        raise ValueError("required_directory_missing") from None
    if (not stat.S_ISDIR(d.st_mode) or d.st_uid != 0
            or stat.S_IMODE(d.st_mode) != 0o700):
        raise ValueError("env_dir_permissions")
    r2uid = pwd.getpwnam("r2relay").pw_uid
    if (not stat.S_ISDIR(c.st_mode) or c.st_uid != r2uid
            or stat.S_IMODE(c.st_mode) != 0o700):
        raise ValueError("cache_dir_permissions")
    key_count = parse_private_env()
    if not UVICORN.is_file():
        raise ValueError("uvicorn_missing")
    if not listening(443) or listening(8443):
        raise ValueError("existing_port_boundary_changed")
    return {"status": "SERVICE_VERIFY_PASS", "env_exists": True,
            "env_mode": "0600", "env_keys_validated": key_count,
            "token_emitted": False, "cache_mode": "0700",
            "unit_matches_template": True, "service_state": systemd_state(),
            "port_443_listening": True, "port_8443_listening": False,
            "port_18085_listening": listening(18085)}

def start():
    previous = verify()
    if previous["service_state"] != "inactive":
        raise ValueError("service_not_inactive")
    if previous["port_18085_listening"]:
        raise ValueError("port_18085_busy")
    prior_443 = bound_addresses(443)
    issued = False
    try:
        issued = True
        command(["/usr/bin/systemctl", "start", SERVICE], timeout=35)
        opener = build_opener(ProxyHandler({}))
        healthy = False
        for _ in range(12):
            try:
                with opener.open("http://127.0.0.1:18085/healthz", timeout=2) as res:
                    healthy = (res.status == 200 and
                               res.read(128).strip() == b'{"status":"ok"}')
            except (OSError, ValueError):
                healthy = False
            if healthy:
                break
            time.sleep(0.5)
        if not healthy or systemd_state() != "active":
            raise ValueError("healthz_not_ready")
        addrs = bound_addresses(18085)
        if not addrs or any(addr != "127.0.0.1:18085" for addr in addrs):
            raise ValueError("listener_not_loopback_only")
        if bound_addresses(443) != prior_443 or listening(8443):
            raise ValueError("port_boundary_changed")
        return {"status": "LOOPBACK_START_PASS", "service_state": "active",
                "health_status": 200, "listener": "127.0.0.1:18085",
                "port_443_listening": True, "port_8443_listening": False,
                "token_emitted": False, "real_r2_tested": False}
    except Exception:
        if issued:
            try:
                command(["/usr/bin/systemctl", "stop", SERVICE], timeout=25)
            except ValueError:
                pass
        raise

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", action="store_true")
    args = parser.parse_args()
    try:
        result = start() if args.start else verify()
    except (OSError, ValueError, UnicodeError, KeyError) as exc:
        result = {"status": "LOOPBACK_CONTROL_FAILED",
                  "reason": str(exc)[:85], "token_emitted": False}
    print(json.dumps(result))
    return 0 if result["status"] in ("SERVICE_VERIFY_PASS", "LOOPBACK_START_PASS") else 1

if __name__ == "__main__":
    raise SystemExit(main())

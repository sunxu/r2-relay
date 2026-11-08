#!/usr/bin/env python3
"""Root-side verification/start for both the legacy and GitHub-release layouts."""
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
RELEASES = Path("/opt/r2-relay-releases")
CURRENT = Path("/opt/r2-relay-current")
LEGACY_SOURCE = Path("/opt/r2-relay/deploy/vps.service")
ENV_DIR = Path("/etc/r2-relay")
ENV_FILE = ENV_DIR / "vps.env"
CACHE = Path("/var/lib/r2-relay/cache")
SERVICE = "r2-relay-vps.service"
SHA = re.compile(r"[0-9a-f]{40}\\Z")
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


def regular(path, mode=None, *, uid=0, gid=None):
    try:
        info = path.lstat()
    except OSError:
        raise ValueError("required_file_missing") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
        raise ValueError("unsafe_file_ownership")
    if gid is not None and info.st_gid != gid:
        raise ValueError("unsafe_file_group")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise ValueError("unsafe_file_mode")
    return info


def active_release(r2_gid):
    try:
        current_info = CURRENT.lstat()
    except OSError:
        raise ValueError("current_pointer_missing") from None
    if not stat.S_ISLNK(current_info.st_mode) or current_info.st_uid != 0:
        raise ValueError("current_pointer_not_root_symlink")
    try:
        releases_root = RELEASES.resolve(strict=True)
        target = os.readlink(CURRENT)
        release = (CURRENT.parent / target).resolve(strict=True)
    except OSError:
        raise ValueError("current_target_unavailable") from None
    if release.parent != releases_root or not release.is_dir():
        raise ValueError("current_target_outside_release_root")
    commit = release.name
    if not SHA.fullmatch(commit):
        raise ValueError("current_release_name_invalid")
    release_info = release.lstat()
    if (release_info.st_uid != 0 or release_info.st_gid != r2_gid
            or stat.S_IMODE(release_info.st_mode) != 0o750):
        raise ValueError("current_release_permissions_invalid")
    marker = release / ".deployment-commit"
    regular(marker, 0o640, gid=r2_gid)
    if marker.read_text(encoding="ascii").strip() != commit:
        raise ValueError("current_release_marker_mismatch")
    unit_source = release / "deploy/vps.service"
    regular(unit_source, 0o640, gid=r2_gid)
    uvicorn = release / ".venv/bin/uvicorn"
    try:
        uvicorn_info = uvicorn.stat()
    except OSError:
        raise ValueError("uvicorn_missing") from None
    if (not stat.S_ISREG(uvicorn_info.st_mode) or uvicorn_info.st_uid != 0
            or uvicorn_info.st_gid != r2_gid or not (uvicorn_info.st_mode & 0o110)):
        raise ValueError("uvicorn_permissions_invalid")
    return release, commit, unit_source, uvicorn


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
    """Inspect all interfaces; the relay itself must remain loopback-only."""
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


def systemd_properties():
    raw = command([
        "/usr/bin/systemctl", "show", SERVICE, "--no-pager",
        "-p", "FragmentPath", "-p", "DropInPaths", "-p", "User",
        "-p", "Group", "-p", "WorkingDirectory", "-p", "EnvironmentFiles",
        "-p", "ExecStart", "-p", "ActiveState",
    ])
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def systemd_state(release):
    props = systemd_properties()
    if props.get("FragmentPath") != str(UNIT):
        raise ValueError("service_unit_path_invalid")
    if props.get("DropInPaths", ""):
        raise ValueError("service_dropins_present")
    if props.get("User") != "r2relay" or props.get("Group") != "r2relay":
        raise ValueError("service_identity_invalid")
    if props.get("WorkingDirectory") != str(CURRENT):
        raise ValueError("service_working_directory_invalid")
    env_spec = props.get("EnvironmentFiles", "")
    if not env_spec.startswith(str(ENV_FILE) + " "):
        raise ValueError("service_environment_file_invalid")
    exec_start = props.get("ExecStart", "")
    if (str(CURRENT / ".venv/bin/uvicorn") not in exec_start
            or str(CURRENT / "vps") not in exec_start
            or "--host 127.0.0.1" not in exec_start
            or "--port 18085" not in exec_start):
        raise ValueError("service_exec_start_invalid")
    if UNIT.read_bytes() != (release / "deploy/vps.service").read_bytes():
        raise ValueError("unit_template_mismatch")
    state = props.get("ActiveState", "")
    if state not in ("active", "inactive"):
        raise ValueError("service_state_unexpected")
    return state


def verify():
    if os.geteuid() != 0:
        raise ValueError("root_required")
    regular(UNIT, 0o644)
    try:
        d = ENV_DIR.lstat()
        c = CACHE.lstat()
        releases_info = RELEASES.lstat()
    except OSError:
        raise ValueError("required_directory_missing") from None
    if (not stat.S_ISDIR(d.st_mode) or d.st_uid != 0
            or stat.S_IMODE(d.st_mode) != 0o700):
        raise ValueError("env_dir_permissions")
    r2 = pwd.getpwnam("r2relay")
    if (not stat.S_ISDIR(c.st_mode) or c.st_uid != r2.pw_uid
            or c.st_gid != r2.pw_gid or stat.S_IMODE(c.st_mode) != 0o700):
        raise ValueError("cache_dir_permissions")
    if (not stat.S_ISDIR(releases_info.st_mode) or releases_info.st_uid != 0
            or releases_info.st_gid != r2.pw_gid
            or stat.S_IMODE(releases_info.st_mode) != 0o750):
        raise ValueError("release_root_permissions_invalid")
    release, commit, _, uvicorn = active_release(r2.pw_gid)
    key_count = parse_private_env()
    state = systemd_state(release)
    if not listening(443) or not listening(8443):
        raise ValueError("expected_443_and_8443_listeners_missing")
    relay_addresses = bound_addresses(18085)
    if state == "active":
        if not relay_addresses or any(addr != "127.0.0.1:18085" for addr in relay_addresses):
            raise ValueError("relay_listener_not_loopback_only")
    elif relay_addresses:
        raise ValueError("inactive_service_port_18085_busy")
    return {
        "status": "SERVICE_VERIFY_PASS",
        "release_commit": commit,
        "release_path": str(release),
        "env_exists": True,
        "env_mode": "0600",
        "env_keys_validated": key_count,
        "token_emitted": False,
        "cache_mode": "0700",
        "unit_matches_release_template": True,
        "service_state": state,
        "port_443_listening": True,
        "port_8443_listening": True,
        "port_18085_listening": bool(relay_addresses),
        "port_18085_loopback_only": state == "active",
        "real_r2_tested": False,
    }


def start():
    previous = verify()
    if previous["service_state"] != "inactive":
        raise ValueError("service_not_inactive")
    if previous["port_18085_listening"]:
        raise ValueError("port_18085_busy")
    prior_443 = bound_addresses(443)
    prior_8443 = bound_addresses(8443)
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
        if not healthy or systemd_state(CURRENT.resolve(strict=True))[0:0]:
            # The empty slice is never true; state is checked explicitly below.
            pass
        if not healthy or systemd_properties().get("ActiveState") != "active":
            raise ValueError("healthz_not_ready")
        addrs = bound_addresses(18085)
        if not addrs or any(addr != "127.0.0.1:18085" for addr in addrs):
            raise ValueError("listener_not_loopback_only")
        if bound_addresses(443) != prior_443 or bound_addresses(8443) != prior_8443:
            raise ValueError("port_boundary_changed")
        return {"status": "LOOPBACK_START_PASS", "service_state": "active",
                "health_status": 200, "listener": "127.0.0.1:18085",
                "port_443_listening": True, "port_8443_listening": True,
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

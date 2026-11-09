#!/usr/bin/env python3
"""Root-only R2 Relay action daemon, accessible through an allowlisted Unix socket."""
import grp
import json
import os
from pathlib import Path
import pwd
import socket
import struct
import subprocess
import syslog

SOCKET = "/run/r2-rootd/control.sock"
ACTIONS = frozenset(("probe", "install_prereqs", "prepare_dirs", "relay_status",
                     "r2_install_dry_run", "r2_install_apply",
                     "r2_runtime_smoke", "r2_service_prepare",
                     "r2_service_verify", "r2_loopback_start",
                     "r2_https_preflight", "r2_https_prepare",
                     "r2_https_start", "r2_https_ca_fingerprint",
                     "r2_deployment_status"))
MAX_REQUEST = 2048

def run(argv, timeout=180, max_output=2500):
    try:
        p = subprocess.run(argv, shell=False, capture_output=True, text=True,
                           errors="replace", timeout=timeout, check=False,
                           env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                                "DEBIAN_FRONTEND": "noninteractive", "LANG": "C"})
        return {"exit_code": p.returncode, "stdout": p.stdout[-max_output:],
                "stderr": p.stderr[-1000:]}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"exit_code": 1, "error_type": type(e).__name__}

def execute(action):
    if action == "probe":
        return {
            "python": run(["/usr/bin/python3", "--version"]),
            "git": run(["/usr/bin/which", "git"]),
            "disk": run(["/usr/bin/df", "-h", "/"]),
            "relay": run(["/usr/bin/systemctl", "show", "r2-relay-vps.service",
                          "--property=LoadState", "--property=ActiveState"]),
        }
    if action == "relay_status":
        return run(["/usr/bin/systemctl", "show", "r2-relay-vps.service",
                    "--property=LoadState", "--property=ActiveState"])
    if action == "install_prereqs":
        return run(["/usr/bin/apt-get", "install", "-y", "git", "python3-venv",
                    "python3-pip", "ca-certificates"], timeout=300)
    if action == "prepare_dirs":
        existing = run(["/usr/bin/id", "r2relay"])
        if existing["exit_code"] != 0:
            created = run(["/usr/sbin/useradd", "--system", "--home-dir",
                           "/var/lib/r2-relay", "--shell", "/usr/sbin/nologin",
                           "r2relay"])
            if created["exit_code"] != 0:
                return created
        return run(["/usr/bin/install", "-d", "-o", "r2relay", "-g", "r2relay",
                    "-m", "0700", "/var/lib/r2-relay/cache"])
    if action in ("r2_install_dry_run", "r2_install_apply"):
        args = ["/usr/bin/python3", "/usr/local/libexec/r2_install_prepare.py"]
        if action == "r2_install_apply":
            args.append("--apply")
        return run(args, timeout=660)
    if action == "r2_runtime_smoke":
        return run(["/usr/sbin/runuser", "-u", "r2relay", "--",
                    "/opt/r2-relay/.venv/bin/python",
                    "/usr/local/libexec/r2_runtime_smoke.py"], timeout=60)
    if action == "r2_service_prepare":
        return run(["/usr/bin/python3",
                    "/usr/local/libexec/r2_service_prepare.py"], timeout=35)
    if action in ("r2_service_verify", "r2_loopback_start"):
        argv = ["/usr/bin/python3", "/usr/local/libexec/r2_loopback_control.py"]
        if action == "r2_loopback_start":
            argv.append("--start")
        return run(argv, timeout=90)
    if action == "r2_deployment_status":
        inspect = run(["/usr/bin/python3", "/opt/r2-relay/scripts/deployment_inspect.py"],
                      timeout=30, max_output=10000)
        verify = run(["/usr/bin/python3", "/usr/local/libexec/r2_loopback_control.py"],
                     timeout=90, max_output=4000)
        try:
            inspection = json.loads(inspect.get("stdout", ""))
            verification = json.loads(verify.get("stdout", ""))
        except (TypeError, ValueError):
            return {"schema": "r2_relay_unified_deployment_status_v1",
                    "status": "UNIFIED_DEPLOYMENT_STATUS_FAILED",
                    "reason": "subcheck_output_invalid", "token_emitted": False}
        current = inspection.get("current_release", {})
        env = inspection.get("files", {}).get("/etc/r2-relay/vps.env", {})
        commit_matches = (current.get("status") == "VALID"
                          and current.get("commit") == verification.get("release_commit"))
        env_matches = (env.get("exists") is True
                       and verification.get("env_exists") is True)
        passed = (inspect.get("exit_code") == 0 and verify.get("exit_code") == 0
                  and verification.get("status") == "SERVICE_VERIFY_PASS"
                  and commit_matches and env_matches
                  and inspection.get("systemd", {}).get("release_layout") is True)
        return {
            "schema": "r2_relay_unified_deployment_status_v1",
            "status": "UNIFIED_DEPLOYMENT_STATUS_PASS" if passed else "UNIFIED_DEPLOYMENT_STATUS_FAILED",
            "release_commit": verification.get("release_commit"),
            "release_matches": commit_matches,
            "environment_file_exists": env_matches,
            "inspection": inspection,
            "service_verification": verification,
            "token_emitted": False,
            "credentials_read": False,
            "modified": False,
            "real_r2_tested": False,
        }
    if action in ("r2_https_preflight", "r2_https_prepare",
                  "r2_https_start", "r2_https_ca_fingerprint"):
        modes = {"r2_https_preflight": "--preflight",
                 "r2_https_prepare": "--prepare",
                 "r2_https_start": "--start",
                 "r2_https_ca_fingerprint": "--ca-fingerprint"}
        return run(["/usr/bin/python3", "/usr/local/libexec/r2_https_ip_control.py",
                    modes[action]], timeout=180)
    raise ValueError("action_not_allowed")

def process_request(payload, uid, authorized_uid):
    if uid != authorized_uid:
        return {"error": "unauthorized_peer"}
    def unique_pairs(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate_json_key")
            out[key] = value
        return out
    try:
        data = json.loads(payload, object_pairs_hook=unique_pairs)
    except (UnicodeError, ValueError, TypeError):
        return {"error": "malformed_request"}
    if not isinstance(data, dict) or set(data) != {"action"}:
        return {"error": "invalid_request_schema"}
    action = data["action"]
    if not isinstance(action, str) or action not in ACTIONS:
        return {"error": "action_not_allowed"}
    syslog.syslog(syslog.LOG_NOTICE, f"r2-rootd action={action} peer_uid={uid}")
    return execute(action)

def main():
    if os.geteuid() != 0:
        raise SystemExit("root required")
    peer_uid = pwd.getpwnam("mcp-relay").pw_uid
    peer_gid = grp.getgrnam("mcp-relay").gr_gid
    path = Path(SOCKET)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    os.chown(path.parent, 0, peer_gid)
    os.chmod(path.parent, 0o750)
    if path.exists() or path.is_symlink():
        raise SystemExit("socket path exists; inspect before removing")
    old_umask = os.umask(0o177)
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET)
    finally:
        os.umask(old_umask)
    os.chown(SOCKET, 0, peer_gid)
    os.chmod(SOCKET, 0o660)
    server.listen(8)
    syslog.openlog("r2-rootd")
    try:
        while True:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(8)
                try:
                    creds = connection.getsockopt(socket.SOL_SOCKET,
                                                 socket.SO_PEERCRED, 12)
                    _, uid, _ = struct.unpack("3i", creds)
                    request = connection.recv(MAX_REQUEST + 1)
                    if len(request) > MAX_REQUEST:
                        response = {"error": "request_too_large"}
                    else:
                        response = process_request(request.decode("utf-8"), uid,
                                                   peer_uid)
                except (OSError, UnicodeError, ValueError):
                    response = {"error": "request_error"}
                try:
                    connection.sendall(json.dumps(response).encode() + b"\n")
                except OSError:
                    pass
    finally:
        server.close()
        path.unlink(missing_ok=True)

if __name__ == "__main__":
    main()

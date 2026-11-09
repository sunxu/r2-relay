#!/usr/bin/env python3
"""One-time trusted upgrade for R2 Relay inspection and runtime-control tools."""
from __future__ import annotations
import argparse
import grp
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.request import urlopen

FILES = {
    "ops/r2_rootd.py": Path("/usr/local/libexec/r2-rootd.py"),
    "ops/r2_loopback_control.py": Path("/usr/local/libexec/r2_loopback_control.py"),
    "ops/r2relay_deploy_receiver.py": Path("/usr/local/libexec/r2relay_deploy_receiver.py"),
    "ops/generic_vps_mcp.py": Path("/opt/r2-mcp/generic_vps_mcp.py"),
    # The legacy connector executes this fixed inspection module from the
    # retained /opt/r2-relay tree. Keep it read-only and update it atomically.
    "ops/deployment_inspect.py": Path("/opt/r2-relay/scripts/deployment_inspect.py"),
}
BACKUP_ROOT = Path("/var/backups/vps-mcp/r2-loopback-control-upgrade")
SHA = re.compile(r"[0-9a-f]{40}\Z")
SERVICES = ("r2-rootd.service", "r2-mcp.service")
RECEIVER_MODE = 0o755


def get_files(commit: str) -> dict:
    if not SHA.fullmatch(commit):
        raise ValueError("full_commit_sha_required")
    urlbase = "https://raw.githubusercontent.com/sunxu/r2-relay/" + commit
    outputs = {}
    for repo_path in FILES:
        with urlopen(urlbase + "/" + repo_path, timeout=25) as response:
            data = response.read(200000)
        if len(data) >= 200000:
            raise ValueError("module_too_large")
        compile(data, repo_path, "exec")
        outputs[repo_path] = data
    return outputs


def invoke(argv):
    subprocess.run(argv, shell=False, check=True, timeout=30,
                   stdin=subprocess.DEVNULL)


def atomically_install(dest: Path, data: bytes, group: int):
    fd, temp = tempfile.mkstemp(prefix=".mcp-upgrade-", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temp, 0, group)
        mode = 0o640 if dest.name == "generic_vps_mcp.py" else (0o755 if dest.name == "r2relay_deploy_receiver.py" else 0o644)
        os.chmod(temp, mode)
        os.replace(temp, dest)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def create_backup_dir(expected_uid=0, expected_gid=0):
    if BACKUP_ROOT.is_symlink():
        raise ValueError("backup_root_symlink")
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_stat = BACKUP_ROOT.lstat()
    if (not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != expected_uid
            or root_stat.st_gid != expected_gid or stat.S_IMODE(root_stat.st_mode) != 0o700):
        raise ValueError("backup_root_permissions_invalid")
    path = BACKUP_ROOT / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                          + "-" + secrets.token_hex(4))
    path.mkdir(mode=0o700)
    return path


def install(commit: str, *, apply: bool):
    if os.geteuid() != 0:
        raise ValueError("root_required")
    contents = get_files(commit)
    for path in FILES.values():
        if path.is_symlink() or not path.parent.is_dir():
            raise ValueError("unsafe_destination")
        if path.exists() and path.lstat().st_uid != 0:
            raise ValueError("existing_file_not_root_owned")
    if not apply:
        return {"status": "UPGRADE_DRY_RUN_PASS", "files": list(FILES),
                "services_restarted": False}
    backup_dir = create_backup_dir()
    existing = {}
    for source, dest in FILES.items():
        existing[source] = dest.exists()
        if dest.exists():
            shutil.copy2(dest, backup_dir / dest.name)
    group = grp.getgrnam("mcp-relay").gr_gid
    try:
        for repo_path, dest in FILES.items():
            atomically_install(dest, contents[repo_path], group)
        invoke(["/usr/bin/python3", "-m", "py_compile",
                *[str(p) for p in FILES.values()]])
        for service in SERVICES:
            invoke(["/usr/bin/systemctl", "restart", service])
            invoke(["/usr/bin/systemctl", "is-active", "--quiet", service])
        return {"status": "UPGRADE_APPLIED", "commit": commit,
                "backup_dir": str(backup_dir), "services_restarted": list(SERVICES)}
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        for source, dest in FILES.items():
            if existing[source]:
                shutil.copy2(backup_dir / dest.name, dest)
            else:
                dest.unlink(missing_ok=True)
        for service in SERVICES:
            try:
                invoke(["/usr/bin/systemctl", "restart", service])
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = install(args.commit, apply=args.apply)
        print(json.dumps(result))
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        print(json.dumps({"status": "FAILED", "error": type(e).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

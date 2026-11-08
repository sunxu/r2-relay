#!/usr/bin/env python3
"""One-time trusted upgrade for R2 runtime smoke and systemd prepare."""
from __future__ import annotations
import argparse
import grp
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.request import urlopen

FILES = {
    "ops/r2_rootd.py": Path("/usr/local/libexec/r2-rootd.py"),
    "ops/r2_runtime_smoke.py": Path("/usr/local/libexec/r2_runtime_smoke.py"),
    "ops/r2_service_prepare.py": Path("/usr/local/libexec/r2_service_prepare.py"),
    "ops/generic_vps_mcp.py": Path("/opt/r2-mcp/generic_vps_mcp.py"),
}
BACKUP_DIR = Path("/var/backups/vps-mcp/r2-runtime-service-upgrade")
SHA = re.compile(r"[0-9a-f]{40}\Z")
SERVICES = ("r2-rootd.service", "r2-mcp.service")

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
        os.chmod(temp, 0o640 if dest.name == "generic_vps_mcp.py" else 0o644)
        os.replace(temp, dest)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def install(commit: str, *, apply: bool):
    if os.geteuid() != 0:
        raise ValueError("root_required")
    contents = get_files(commit)
    for path in FILES.values():
        if path.is_symlink() or not path.parent.is_dir():
            raise ValueError("unsafe_destination")
        if path.exists() and path.stat().st_uid != 0:
            raise ValueError("existing_file_not_root_owned")
    if not apply:
        return {"status": "UPGRADE_DRY_RUN_PASS", "files": list(FILES),
                "services_restarted": False}
    if BACKUP_DIR.exists():
        raise ValueError("backup_already_exists")
    BACKUP_DIR.mkdir(parents=True, mode=0o700)
    os.chmod(BACKUP_DIR, 0o700)
    existing = {}
    for source, dest in FILES.items():
        existing[source] = dest.exists()
        if dest.exists():
            shutil.copy2(dest, BACKUP_DIR / dest.name)
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
                "backup_dir": str(BACKUP_DIR), "tunnel_restarted": False}
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        for source, dest in FILES.items():
            if existing[source]:
                shutil.copy2(BACKUP_DIR / dest.name, dest)
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

#!/usr/bin/env python3
"""Controlled fixed-commit R2 Relay installer: no service start or secret access."""
import argparse
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import shutil
import subprocess
import tarfile

COMMIT = "d6fa3503127e7e8b91076caff187f6ec095ccf60"
STAGE = Path("/var/lib/vps-mcp/workspaces/r2-relay")
DEST = Path("/opt/r2-relay")
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_FILES = 5000
REQUIRED = {"vps/pyproject.toml", "deploy/vps.service",
            "scripts/deployment_preflight.py", "deploy/Caddyfile.example"}

def git_archive(stage=STAGE):
    if stage.is_symlink() or not stage.is_dir():
        raise ValueError("stage_missing_or_symlink")
    argv = ["/usr/bin/git", "-c", f"safe.directory={stage}", "-C", str(stage),
            "archive", "--format=tar", COMMIT]
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=90, check=False, shell=False,
                           env={"PATH": "/usr/bin:/bin", "LANG": "C",
                                "GIT_CONFIG_NOSYSTEM": "1",
                                "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("archive_unavailable") from None
    if p.returncode or len(p.stdout) > MAX_ARCHIVE:
        raise ValueError("archive_invalid_or_too_large")
    return p.stdout

def verify_members(archive):
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tf:
            members = tf.getmembers()
            if len(members) > MAX_FILES:
                raise ValueError("too_many_entries")
            names = set()
            total = 0
            for member in members:
                name = member.name.rstrip("/")
                path = PurePosixPath(name)
                if (not name or path.is_absolute() or ".." in path.parts
                        or name in names or "\\" in name
                        or not (member.isfile() or member.isdir())
                        or member.pax_headers):
                    raise ValueError("unsafe_archive_member")
                names.add(name)
                if member.isfile():
                    total += member.size
                    if total > MAX_ARCHIVE:
                        raise ValueError("archive_unpacked_too_large")
            if not REQUIRED.issubset(names):
                raise ValueError("required_files_missing")
            return {"files": len(members), "bytes": total}
    except (tarfile.TarError, OSError):
        raise ValueError("invalid_archive") from None

def run_as_user(argv, user="r2relay", timeout=360):
    try:
        p = subprocess.run(["/usr/sbin/runuser", "-u", user, "--", *argv],
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,
                           timeout=timeout, check=False, shell=False,
                           env={"HOME": "/var/lib/r2-relay",
                                "PATH": "/usr/bin:/bin",
                                "PIP_NO_INPUT": "1",
                                "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
        return p.returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1

def prepare(dry_run=True, stage=STAGE, dest=DEST):
    if os.geteuid() != 0:
        raise ValueError("root_required")
    if dest.exists() or dest.is_symlink():
        raise ValueError("destination_already_exists")
    archive = git_archive(stage)
    info = verify_members(archive)
    if dry_run:
        return {"status": "DRY_RUN_PASS", "commit": COMMIT,
                "destination_created": False, **info}
    dest.mkdir(mode=0o755)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tf:
            for member in tf.getmembers():
                rel = PurePosixPath(member.name)
                path = dest.joinpath(*rel.parts)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True, mode=0o755)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                stream = tf.extractfile(member)
                if stream is None:
                    raise ValueError("missing_archive_file")
                with path.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=1048576)
                os.chmod(path, 0o644)
        # systemd rootd UMask=0077 overrides mkdir(mode=0755).
        # Make source directories explicitly readable by the runtime user.
        for directory, _, _ in os.walk(dest):
            os.chmod(directory, 0o755)
        venv = dest / ".venv"
        venv.mkdir(mode=0o700)
        owner = pwd.getpwnam("r2relay")
        os.chown(venv, owner.pw_uid, owner.pw_gid)
        if run_as_user(["/usr/bin/python3", "-m", "venv", str(venv)], timeout=120):
            return {"status": "PARTIAL_INSTALL", "step": "venv"}
        # PEP 517 builders may write metadata into source; build from a
        # r2relay-owned temporary copy, never modify root-owned runtime files.
        build_src = venv / "build-src"
        shutil.copytree(dest / "vps", build_src)
        shutil.chown(build_src, user="r2relay", group="r2relay")
        for base, dirs, files in os.walk(build_src):
            for name in dirs + files:
                os.chown(Path(base)/name, owner.pw_uid, owner.pw_gid)
        if run_as_user([str(venv/"bin/python"), "-m", "pip", "install",
                        "--no-input", str(build_src)], timeout=480):
            return {"status": "PARTIAL_INSTALL", "step": "pip"}
        return {"status": "INSTALLED_NOT_STARTED", "commit": COMMIT,
                "path": str(dest), "service_started": False}
    except (OSError, ValueError):
        return {"status": "PARTIAL_INSTALL", "step": "copy_or_permissions"}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare(dry_run=not args.apply)
    except (OSError, ValueError) as exc:
        result = {"status": "FAILED", "reason": str(exc)[:100]}
    print(json.dumps(result))
    return 0 if result["status"] in ("DRY_RUN_PASS", "INSTALLED_NOT_STARTED") else 1

if __name__ == "__main__":
    raise SystemExit(main())

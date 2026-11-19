#!/usr/bin/python3
"""Forced-command receiver for GitHub Actions R2 Relay deployments.

This program is installed root-owned on the VPS and invoked only through one
restricted SSH key. It accepts exactly one command and a bounded tar stream.
It never runs shell commands from the repository archive.
"""
from __future__ import annotations

import fcntl
import grp
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import urlopen

RELEASES = Path("/opt/r2-relay-releases")
CURRENT = Path("/opt/r2-relay-current")
UNIT = Path("/etc/systemd/system/r2-relay-vps.service")
ENV_FILE = Path("/etc/r2-relay/vps.env")
CACHE = Path("/var/lib/r2-relay/cache")
BACKUPS = Path("/var/backups/r2-relay/deploy")
DOWNLOAD_CONFIG = Path("/etc/r2-relay/download.toml")
DOWNLOAD_CONFIG_BACKUPS = Path("/var/backups/r2-relay/download-config")
LOCK = Path("/run/lock/r2-relay-github-deploy.lock")
SERVICE = "r2-relay-vps.service"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 6000
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DOWNLOAD_CONFIG_MARKER = "r2-relay-download-config-dropin-v1"
DOWNLOAD_CONFIG_TEXT = """schema_version = 1

# 由受控 GitHub Actions 管理；当前只应用关闭配置。
[download]
enabled = false
part_size_mib = 16
split_threshold_mib = 24
cache_ttl_seconds = 600
max_cache_bytes = 0
min_free_bytes = 0
max_active_tasks = 0
max_retained_tasks = 0
max_waiters = 0
wait_timeout_seconds = 30
"""
RECEIVER_BUILD_ID = "r2-relay-deploy-receiver-v5-controlled-download-config"
REQUIRED = {
    "vps/pyproject.toml",
    "vps/r2relay_vps/app.py",
    "deploy/vps.service",
    "deploy/requirements-vps.lock",
    ".deployment-commit",
}

UNIT_TEXT = """[Unit]
Description=R2 Relay VPS API (loopback only; TLS terminates at Caddy 8443)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=r2relay
Group=r2relay
WorkingDirectory=/opt/r2-relay-current
EnvironmentFile=/etc/r2-relay/vps.env
ExecStart=/opt/r2-relay-current/.venv/bin/uvicorn r2relay_vps.app:create_app --factory --app-dir /opt/r2-relay-current/vps --host 127.0.0.1 --port 18085 --workers 1
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/r2-relay/cache
PrivateTmp=true
RuntimeDirectory=r2-relay
RuntimeDirectoryMode=0700
UMask=0077
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
"""

LEGACY_UNIT_TEXT = """[Unit]
Description=R2 Relay VPS API (loopback only; TLS terminates at Caddy 8443)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=r2relay
Group=r2relay
WorkingDirectory=/opt/r2-relay
EnvironmentFile=/etc/r2-relay/vps.env
ExecStart=/opt/r2-relay/.venv/bin/uvicorn r2relay_vps.app:create_app --factory --app-dir /opt/r2-relay/vps --host 127.0.0.1 --port 18085 --workers 1
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/r2-relay/cache
PrivateTmp=true
RuntimeDirectory=r2-relay
RuntimeDirectoryMode=0700
UMask=0077
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
"""


class DeployError(Exception):
    pass


def emit(status: str, **fields) -> None:
    print(json.dumps({"status": status, "receiver_build_id": RECEIVER_BUILD_ID, **fields}, sort_keys=True), flush=True)


def parse_original_command(command: str | None) -> str:
    if not isinstance(command, str):
        raise DeployError("missing_forced_command")
    match = re.fullmatch(r"deploy ([0-9a-f]{40})", command)
    if not match:
        raise DeployError("command_not_allowed")
    return match.group(1)


def read_archive(stream, directory: Path = Path("/var/tmp")) -> Path:
    fd, name = tempfile.mkstemp(prefix="r2-relay-deploy-", suffix=".tar", dir=directory)
    path = Path(name)
    total = 0
    try:
        with os.fdopen(fd, "wb") as output:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise DeployError("archive_too_large")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total == 0:
            raise DeployError("empty_archive")
        os.chmod(path, 0o600)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def validate_archive(archive_path: Path, expected_sha: str):
    if not SHA_RE.fullmatch(expected_sha):
        raise DeployError("invalid_commit_sha")
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_MEMBERS:
                raise DeployError("archive_member_count_invalid")
            names = set()
            total = 0
            marker = None
            for member in members:
                raw_name = member.name
                if not isinstance(raw_name, str) or not raw_name or chr(92) in raw_name:
                    raise DeployError("archive_path_invalid")
                if raw_name.startswith("/"):
                    raise DeployError("archive_absolute_path")
                name = raw_name.rstrip("/")
                path = PurePosixPath(name)
                if (not name or name in names or
                        any(part in ("", ".", "..") for part in path.parts) or
                        str(path) != name):
                    raise DeployError("archive_path_invalid")
                names.add(name)
                if not (member.isfile() or member.isdir()):
                    raise DeployError("archive_special_file_denied")
                if member.mode & 0o7000:
                    raise DeployError("archive_privileged_mode_denied")
                if member.isfile():
                    total += member.size
                    if member.size < 0 or total > MAX_UNPACKED_BYTES:
                        raise DeployError("archive_unpacked_size_exceeded")
                if name == ".deployment-commit":
                    if not member.isfile() or member.size > 64:
                        raise DeployError("commit_marker_invalid")
                    source = archive.extractfile(member)
                    marker = source.read(65).decode("ascii", errors="strict").strip()
            if not REQUIRED.issubset(names):
                raise DeployError("required_files_missing")
            if marker != expected_sha:
                raise DeployError("commit_marker_mismatch")
            if "deploy/download-config-dropin-v1" in names:
                config_marker = archive.extractfile(
                    archive.getmember("deploy/download-config-dropin-v1")
                )
                if config_marker is None or config_marker.read(128).decode("ascii") != DOWNLOAD_CONFIG_MARKER + "\n":
                    raise DeployError("download_config_capability_marker_invalid")
            unit_member = archive.getmember("deploy/vps.service")
            unit_stream = archive.extractfile(unit_member)
            if unit_stream is None or unit_stream.read(65536).decode("utf-8") != UNIT_TEXT:
                raise DeployError("service_template_not_approved")
            return members, {"files": len(members), "unpacked_bytes": total}
    except (tarfile.TarError, OSError, UnicodeError, KeyError):
        raise DeployError("archive_invalid") from None


def extract_archive(archive_path: Path, stage: Path, members, expected_sha: str) -> None:
    if not SHA_RE.fullmatch(expected_sha):
        raise DeployError("invalid_commit_sha")
    stage.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            for member in members:
                name = member.name.rstrip("/")
                target = stage.joinpath(*PurePosixPath(name).parts)
                if member.isdir():
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise DeployError("archive_member_unreadable")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                fd = os.open(target, flags, 0o600)
                with os.fdopen(fd, "wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)

        # The archive marker is validated before extraction but is never trusted
        # as the installed marker. Recreate it from the exact SHA authorized by
        # the forced SSH command so every immutable release carries a verifiable
        # root-owned deployment identity.
        marker = stage / ".deployment-commit"
        marker.unlink(missing_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(marker, flags, 0o640)
        with os.fdopen(fd, "w", encoding="ascii") as output:
            output.write(expected_sha + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(marker, 0o640)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def validate_host_state() -> tuple[int, int, str]:
    if os.geteuid() != 0:
        raise DeployError("root_required")
    try:
        account = pwd.getpwnam("r2relay")
        group = grp.getgrnam("r2relay")
    except KeyError:
        raise DeployError("runtime_account_missing") from None
    if ENV_FILE.is_symlink() or not ENV_FILE.is_file():
        raise DeployError("production_env_missing_or_unsafe")
    env_stat = ENV_FILE.stat()
    if env_stat.st_uid != 0 or env_stat.st_mode & 0o077:
        raise DeployError("production_env_permissions_invalid")
    if CACHE.is_symlink() or not CACHE.is_dir():
        raise DeployError("cache_directory_missing_or_unsafe")
    cache_stat = CACHE.stat()
    if cache_stat.st_uid != account.pw_uid or cache_stat.st_gid != group.gr_gid or cache_stat.st_mode & 0o077:
        raise DeployError("cache_directory_owner_or_permissions_invalid")
    if UNIT.is_symlink() or not UNIT.is_file():
        raise DeployError("service_unit_missing_or_unsafe")
    unit_stat = UNIT.stat()
    if unit_stat.st_uid != 0 or unit_stat.st_mode & 0o022:
        raise DeployError("service_unit_permissions_invalid")
    unit_text = UNIT.read_text(encoding="utf-8")
    if unit_text.strip() not in (LEGACY_UNIT_TEXT.strip(), UNIT_TEXT.strip()):
        raise DeployError("installed_service_unit_not_recognized")
    active = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", SERVICE],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=10,
    )
    if active.returncode != 0:
        raise DeployError("service_not_active_before_deploy")
    if CURRENT.exists() and not CURRENT.is_symlink():
        raise DeployError("current_path_not_symlink")
    previous_target = ""
    if CURRENT.is_symlink():
        target = os.readlink(CURRENT)
        resolved = (CURRENT.parent / target).resolve(strict=True)
        if resolved.parent != RELEASES.resolve(strict=True) or not resolved.is_dir():
            raise DeployError("current_target_outside_releases")
        previous_target = str(resolved)
    return account.pw_uid, group.gr_gid, previous_target


def run_fixed(argv: list[str], timeout: int = 120) -> None:
    result = subprocess.run(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, errors="replace",
        check=False, timeout=timeout, shell=False,
        env={
            "HOME": "/root",
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C",
            "PIP_NO_INPUT": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "DEBIAN_FRONTEND": "noninteractive",
        },
    )
    if result.returncode != 0:
        raise DeployError("command_failed:" + Path(argv[0]).name)


def is_expected_venv_lib64_symlink(root: Path, path: Path) -> bool:
    """Allow only Python venv's local .venv/lib64 -> lib directory link."""
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return False
    target = root / ".venv/lib"
    return (
        relative == ".venv/lib64"
        and path.is_symlink()
        and os.readlink(path) == "lib"
        and target.is_dir()
        and not target.is_symlink()
    )

def chown_release(root: Path, gid: int) -> None:
    for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
        base = Path(current)
        for name in files:
            path = base / name
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            os.chown(path, 0, gid, follow_symlinks=False)
            os.chmod(path, 0o750 if mode & 0o111 else 0o640)
        for name in dirs:
            path = base / name
            if path.is_symlink():
                if is_expected_venv_lib64_symlink(root, path):
                    continue
                raise DeployError("release_symlink_unexpected")
            os.chown(path, 0, gid, follow_symlinks=False)
            os.chmod(path, 0o750)
        os.chown(base, 0, gid, follow_symlinks=False)
        os.chmod(base, 0o750)


def chown_build_tree(root: Path, uid: int, gid: int) -> None:
    """Make a disposable source copy writable by the unprivileged build user."""
    for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
        base = Path(current)
        for name in files:
            path = base / name
            if path.is_symlink():
                raise DeployError("build_source_symlink_unexpected")
            mode = path.stat().st_mode
            os.chown(path, uid, gid, follow_symlinks=False)
            os.chmod(path, 0o700 if mode & 0o111 else 0o600)
        for name in dirs:
            path = base / name
            if path.is_symlink():
                raise DeployError("build_source_symlink_unexpected")
            os.chown(path, uid, gid, follow_symlinks=False)
            os.chmod(path, 0o700)
        os.chown(base, uid, gid, follow_symlinks=False)
        os.chmod(base, 0o700)


def run_as_r2relay(
    argv: list[str], timeout: int = 120, *, step: str = "runtime_user_command",
) -> None:
    result = subprocess.run(
        ["/usr/sbin/runuser", "-u", "r2relay", "--", *argv],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, errors="replace",
        check=False, timeout=timeout, shell=False,
        env={
            "HOME": "/var/lib/r2-relay",
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "PIP_NO_INPUT": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
        },
    )
    if result.returncode != 0:
        raise DeployError(f"runtime_user_command_failed:{step}:exit_{result.returncode}")


def prepare_release(stage: Path, uid: int, gid: int) -> None:
    # Source is root-owned/read-only to the service account. Build and install
    # Python packages as r2relay so repository-controlled build hooks never run
    # with root privileges.
    chown_release(stage, gid)
    venv = stage / ".venv"
    venv.mkdir(mode=0o750)
    os.chown(venv, uid, gid)
    run_as_r2relay(["/usr/bin/python3.11", "-m", "venv", str(venv)], timeout=120, step="venv_create")
    pip = str(venv / "bin/pip")
    run_as_r2relay([
        pip, "install", "--no-input", "--disable-pip-version-check",
        "--no-cache-dir", "-r", str(stage / "deploy/requirements-vps.lock"),
    ], timeout=600, step="pip_requirements")
    # setuptools writes build/ into the project source tree. Keep the actual
    # release root-owned and read-only by building from a disposable writable
    # copy; repository-controlled build hooks still execute only as r2relay.
    build_root = Path(tempfile.mkdtemp(prefix="r2-relay-build-", dir="/var/tmp"))
    try:
        os.chown(build_root, uid, gid)
        os.chmod(build_root, 0o700)
        build_source = build_root / "vps"
        shutil.copytree(stage / "vps", build_source, symlinks=False)
        chown_build_tree(build_source, uid, gid)
        run_as_r2relay([
            pip, "install", "--no-input", "--disable-pip-version-check",
            "--no-deps", "--no-build-isolation", str(build_source),
        ], timeout=300, step="pip_project")
    finally:
        shutil.rmtree(build_root, ignore_errors=True)
    run_as_r2relay([
        str(venv / "bin/python"), "-c",
        "import fastapi, uvicorn, r2relay_vps.app; print('runtime-import-ok')",
    ], timeout=30, step="runtime_import")
    chown_release(stage, gid)


def validate_existing_release(
    release: Path, gid: int, owner_uid: int = 0,
    python_executable: Path = Path("/usr/bin/python3.11"),
) -> None:
    """Validate a previously built release before allowing rollback/reuse."""
    def check_directory(path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            raise DeployError("existing_release_directory_invalid")
        info = path.lstat()
        if info.st_uid != owner_uid or info.st_gid != gid or info.st_mode & 0o027:
            raise DeployError("existing_release_directory_permissions_invalid")

    check_directory(release)
    venv = release / ".venv"
    check_directory(venv)
    bindir = venv / "bin"
    check_directory(bindir)
    check_directory(release / "vps")
    check_directory(release / "vps/r2relay_vps")

    allowed_python_links = {
        ".venv/bin/python",
        ".venv/bin/python3",
        ".venv/bin/python3.11",
    }
    for current, dirs, files in os.walk(release, topdown=True, followlinks=False):
        base = Path(current)
        for name in dirs:
            path = base / name
            if is_expected_venv_lib64_symlink(release, path):
                continue
            check_directory(path)
        for name in files:
            path = base / name
            relative = path.relative_to(release).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                if relative not in allowed_python_links:
                    raise DeployError("existing_release_symlink_unexpected")
                try:
                    if path.resolve(strict=True) != python_executable.resolve(strict=True):
                        raise DeployError("existing_release_python_link_invalid")
                except OSError:
                    raise DeployError("existing_release_python_link_invalid") from None
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
                    or info.st_gid != gid or info.st_mode & 0o027):
                raise DeployError("existing_release_file_permissions_invalid")

    python = bindir / "python"
    uvicorn = bindir / "uvicorn"
    app = release / "vps/r2relay_vps/app.py"
    for path in (python, uvicorn, app):
        if not path.exists() or not path.is_file():
            raise DeployError("existing_release_runtime_file_missing")
    if not (uvicorn.stat().st_mode & 0o110):
        raise DeployError("existing_release_uvicorn_not_executable")
    run_as_r2relay([
        str(python), "-c",
        "import fastapi, uvicorn, r2relay_vps.app; print('runtime-import-ok')",
    ], timeout=30)


def write_atomic(path: Path, content: str, mode: int = 0o644) -> None:
    fd, name = tempfile.mkstemp(prefix=".r2-deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chown(name, 0, 0)
        os.chmod(name, mode)
        os.replace(name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        Path(name).unlink(missing_ok=True)


def set_current(target: str | None) -> None:
    if target is None:
        CURRENT.unlink(missing_ok=True)
    else:
        temp = CURRENT.parent / (".r2-relay-current-" + str(os.getpid()))
        temp.unlink(missing_ok=True)
        os.symlink(target, temp)
        os.replace(temp, CURRENT)
    dir_fd = os.open(CURRENT.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def health_check(attempts: int = 25) -> bool:
    for _ in range(attempts):
        try:
            with urlopen("http://127.0.0.1:18085/healthz", timeout=2) as response:
                if response.status == 200:
                    payload = response.read(1024)
                    if b'"status"' in payload and b'"ok"' in payload:
                        return True
        except Exception:
            pass
        time.sleep(1)
    return False


def restore(backup_unit: Path, previous_target: str, previous_was_symlink: bool) -> bool:
    try:
        backup_mode = stat.S_IMODE(backup_unit.stat().st_mode)
        write_atomic(UNIT, backup_unit.read_text(encoding="utf-8"), backup_mode)
        if previous_was_symlink:
            set_current(previous_target)
        else:
            set_current(None)
        run_fixed(["/usr/bin/systemctl", "daemon-reload"], timeout=30)
        run_fixed(["/usr/bin/systemctl", "restart", SERVICE], timeout=90)
        return health_check(attempts=20)
    except Exception:
        return False


def open_deploy_lock() -> int:
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(LOCK, flags, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o077 or info.st_nlink != 1):
        os.close(fd)
        raise DeployError("deployment_lock_file_unsafe")
    return fd


def fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def new_staging_path(sha: str) -> Path:
    if not SHA_RE.fullmatch(sha):
        raise DeployError("invalid_commit_sha")
    return RELEASES / (".staging-" + sha + "-" + secrets.token_hex(8))

def deploy(sha: str, archive_path: Path) -> dict:
    if not SHA_RE.fullmatch(sha):
        raise DeployError("invalid_commit_sha")
    lock_fd = open_deploy_lock()
    with os.fdopen(lock_fd, "r+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError("deployment_already_running") from None

        uid, gid, previous_target = validate_host_state()
        previous_was_symlink = CURRENT.is_symlink()
        release = RELEASES / sha
        reuse_existing_release = False
        try:
            release.lstat()
        except FileNotFoundError:
            pass
        else:
            if release.is_symlink() or not release.is_dir():
                raise DeployError("release_path_unsafe")
            validate_existing_release(release, gid)
            if previous_was_symlink and Path(previous_target) == release.resolve(strict=True):
                return {"status": "ALREADY_DEPLOYED", "commit": sha}
            reuse_existing_release = True
        if RELEASES.is_symlink():
            raise DeployError("release_root_symlink")
        if RELEASES.exists():
            releases_stat = RELEASES.lstat()
            if not stat.S_ISDIR(releases_stat.st_mode) or releases_stat.st_uid != 0 or releases_stat.st_mode & 0o022:
                raise DeployError("release_root_permissions_invalid")
            if releases_stat.st_gid != gid:
                os.chown(RELEASES, 0, gid)
        else:
            RELEASES.mkdir(mode=0o750, parents=False, exist_ok=False)
            os.chown(RELEASES, 0, gid)
        os.chmod(RELEASES, 0o750)

        members, archive_info = validate_archive(archive_path, sha)
        if not reuse_existing_release:
            stage = new_staging_path(sha)
            extract_archive(archive_path, stage, members, sha)
            try:
                # Move source to its immutable final path before creating the venv:
                # console-script shebangs embed the absolute venv path.
                os.rename(stage, release)
                prepare_release(release, uid, gid)
            except Exception:
                shutil.rmtree(stage, ignore_errors=True)
                shutil.rmtree(release, ignore_errors=True)
                raise

        if BACKUPS.is_symlink():
            raise DeployError("backup_root_symlink")
        BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup_root_stat = BACKUPS.stat()
        if not stat.S_ISDIR(backup_root_stat.st_mode) or backup_root_stat.st_uid != 0:
            raise DeployError("backup_root_permissions_invalid")
        os.chmod(BACKUPS, 0o700)
        backup_dir = BACKUPS / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + sha[:12] + "-" + secrets.token_hex(4))
        backup_dir.mkdir(mode=0o700)
        backup_unit = backup_dir / "r2-relay-vps.service"
        shutil.copy2(UNIT, backup_unit)
        state = {
            "requested_commit": sha,
            "previous_target": previous_target,
            "previous_was_symlink": previous_was_symlink,
        }
        state_path = backup_dir / "state.json"
        with state_path.open("w", encoding="utf-8") as output:
            output.write(json.dumps(state, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(state_path, 0o600)
        with backup_unit.open("rb") as backup_stream:
            os.fsync(backup_stream.fileno())
        fsync_path(backup_dir)
        fsync_path(BACKUPS)

        try:
            write_atomic(UNIT, UNIT_TEXT)
            set_current(str(release))
            run_fixed(["/usr/bin/systemctl", "daemon-reload"], timeout=30)
            run_fixed(["/usr/bin/systemctl", "restart", SERVICE], timeout=90)
            if not health_check():
                raise DeployError("new_release_health_check_failed")
            return {
                "status": "DEPLOYED",
                "commit": sha,
                "previous_commit": Path(previous_target).name if previous_target else "legacy-layout",
                "release": str(release),
                "backup": str(backup_dir),
                "archive_files": archive_info["files"],
                "archive_bytes": archive_info["unpacked_bytes"],
                "health": "PASS",
                "caddy_modified": False,
                "cache_modified": False,
                "environment_modified": False,
            }
        except Exception as exc:
            rolled_back = restore(backup_unit, previous_target, previous_was_symlink)
            if rolled_back:
                raise DeployError("deployment_failed_rolled_back:" + str(exc)) from None
            raise DeployError("deployment_failed_rollback_failed:" + str(exc)) from None


def parse_download_config_command(command: str | None) -> str:
    if not isinstance(command, str):
        raise DeployError("missing_forced_command")
    match = re.fullmatch(r"download-config disabled ([0-9a-f]{40})", command)
    if not match:
        raise DeployError("download_config_command_not_allowed")
    return match.group(1)


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _validate_download_config_paths(gid: int) -> None:
    parent = DOWNLOAD_CONFIG.parent
    if parent.is_symlink() or not parent.is_dir():
        raise DeployError("download_config_parent_missing_or_unsafe")
    parent_stat = parent.stat()
    if parent_stat.st_uid != 0 or parent_stat.st_mode & 0o022:
        raise DeployError("download_config_parent_permissions_invalid")
    if DOWNLOAD_CONFIG.is_symlink():
        raise DeployError("download_config_path_symlink")
    if DOWNLOAD_CONFIG.exists():
        info = DOWNLOAD_CONFIG.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_gid not in (0, gid) or info.st_mode & 0o027
                or info.st_nlink != 1 or info.st_size > 4096):
            raise DeployError("download_config_file_permissions_invalid")
    backup_parent = DOWNLOAD_CONFIG_BACKUPS.parent
    if backup_parent.is_symlink() or not backup_parent.is_dir():
        raise DeployError("download_config_backup_parent_missing_or_unsafe")
    backup_parent_stat = backup_parent.stat()
    if backup_parent_stat.st_uid != 0 or backup_parent_stat.st_mode & 0o022:
        raise DeployError("download_config_backup_parent_permissions_invalid")
    if DOWNLOAD_CONFIG_BACKUPS.is_symlink():
        raise DeployError("download_config_backup_root_symlink")


def _atomic_write_download_config(content: bytes, gid: int) -> None:
    fd, name = tempfile.mkstemp(prefix=".r2-download-config-", dir=DOWNLOAD_CONFIG.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            os.fchown(output.fileno(), 0, gid)
            os.fchmod(output.fileno(), 0o640)
        os.replace(name, DOWNLOAD_CONFIG)
        _fsync_directory(DOWNLOAD_CONFIG.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _restore_download_config(previous: bytes | None, gid: int) -> None:
    if previous is None:
        DOWNLOAD_CONFIG.unlink(missing_ok=True)
        _fsync_directory(DOWNLOAD_CONFIG.parent)
    else:
        _atomic_write_download_config(previous, gid)


def apply_download_config_disabled(sha: str) -> dict:
    if not SHA_RE.fullmatch(sha):
        raise DeployError("invalid_commit_sha")
    if os.geteuid() != 0:
        raise DeployError("root_required")
    lock_fd = open_deploy_lock()
    with os.fdopen(lock_fd, "r+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError("deployment_already_running") from None

        uid, gid, _previous_target = validate_host_state()

        _validate_download_config_paths(gid)
        expected = DOWNLOAD_CONFIG_TEXT.encode("utf-8")
        previous = DOWNLOAD_CONFIG.read_bytes() if DOWNLOAD_CONFIG.exists() else None
        if previous == expected:
            if not health_check():
                raise DeployError("download_config_existing_health_check_failed")
            return {
                "status": "DOWNLOAD_CONFIG_ALREADY_APPLIED",
                "commit": sha,
                "download_enabled": False,
                "config_path": str(DOWNLOAD_CONFIG),
                "service_restarted": False,
            }

        DOWNLOAD_CONFIG_BACKUPS.mkdir(mode=0o700, parents=False, exist_ok=True)
        backup_root_stat = DOWNLOAD_CONFIG_BACKUPS.lstat()
        if (not stat.S_ISDIR(backup_root_stat.st_mode)
                or backup_root_stat.st_uid != 0 or backup_root_stat.st_mode & 0o077):
            raise DeployError("download_config_backup_root_permissions_invalid")
        backup = DOWNLOAD_CONFIG_BACKUPS / (
            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)
        )
        backup.mkdir(mode=0o700)
        state = {
            "requested_commit": sha,
            "previous_config_existed": previous is not None,
            "config_path": str(DOWNLOAD_CONFIG),
        }
        if previous is not None:
            previous_backup = backup / "download.toml"
            previous_backup.write_bytes(previous)
            with previous_backup.open("rb") as backup_stream:
                os.fsync(backup_stream.fileno())
            os.chown(previous_backup, 0, 0)
            os.chmod(previous_backup, 0o600)
        state_path = backup / "state.json"
        state_path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        with state_path.open("rb") as state_stream:
            os.fsync(state_stream.fileno())
        os.chown(state_path, 0, 0)
        os.chmod(state_path, 0o600)
        _fsync_directory(backup)
        _fsync_directory(DOWNLOAD_CONFIG_BACKUPS)

        try:
            _atomic_write_download_config(expected, gid)
            run_fixed(["/usr/bin/systemctl", "restart", SERVICE], timeout=90)
            if not health_check():
                raise DeployError("download_config_health_check_failed")
            return {
                "status": "DOWNLOAD_CONFIG_APPLIED",
                "commit": sha,
                "download_enabled": False,
                "config_path": str(DOWNLOAD_CONFIG),
                "config_mode": "0640",
                "backup": str(backup),
                "service_restarted": True,
                "cache_modified": False,
                "environment_modified": False,
                "caddy_modified": False,
            }
        except Exception as exc:
            try:
                _restore_download_config(previous, gid)
                run_fixed(["/usr/bin/systemctl", "restart", SERVICE], timeout=90)
                if not health_check():
                    raise DeployError("download_config_rollback_health_check_failed")
            except Exception:
                raise DeployError(
                    "download_config_apply_failed_rollback_failed:" + type(exc).__name__
                ) from None
            raise DeployError(
                "download_config_apply_failed_rolled_back:" + type(exc).__name__
            ) from None


def main() -> int:
    try:
        original_command = os.environ.get("SSH_ORIGINAL_COMMAND")
        if isinstance(original_command, str) and original_command.startswith("download-config"):
            sha = parse_download_config_command(original_command)
            if sys.stdin.buffer.read(1):
                raise DeployError("download_config_stdin_must_be_empty")
            result = apply_download_config_disabled(sha)
            emit(result.pop("status"), **result)
            return 0

        sha = parse_original_command(original_command)
        archive = read_archive(sys.stdin.buffer)
        try:
            result = deploy(sha, archive)
            emit(result.pop("status"), **result)
            return 0
        finally:
            archive.unlink(missing_ok=True)
    except DeployError as exc:
        emit("REJECTED_OR_FAILED", reason=str(exc)[:160])
        return 1
    except Exception as exc:
        emit("REJECTED_OR_FAILED", reason="internal_error:" + type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

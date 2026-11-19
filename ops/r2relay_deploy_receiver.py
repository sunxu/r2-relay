#!/usr/bin/python3
"""Forced-command receiver for GitHub Actions R2 Relay deployments.

This program is installed root-owned on the VPS and invoked only through one
restricted SSH key. It accepts exactly one command and a bounded tar stream.
It never runs shell commands from the repository archive.
"""
from __future__ import annotations

import fcntl
from datetime import datetime, timezone
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
DOWNLOAD_CACHE = Path("/var/lib/r2-relay/downloads")
BACKUPS = Path("/var/backups/r2-relay/deploy")
DOWNLOAD_CONFIG_DIR = Path("/etc/r2-relay-download")
DOWNLOAD_CONFIG = DOWNLOAD_CONFIG_DIR / "download.toml"
DOWNLOAD_CONFIG_BACKUPS = Path("/var/backups/r2-relay/download-config")
DOWNLOAD_TEST_STATE_DIR = Path("/var/lib/r2-relay-deploy")
DOWNLOAD_TEST_LEASE = DOWNLOAD_TEST_STATE_DIR / "download-test-lease.json"
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
RECEIVER_INSTALLED = Path("/usr/local/libexec/r2relay_deploy_receiver.py")
LOCK = Path("/run/lock/r2-relay-github-deploy.lock")
SERVICE = "r2-relay-vps.service"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 6000
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DOWNLOAD_CONFIG_MARKER = "r2-relay-download-config-dropin-v1"
DOWNLOAD_TEST_CAPABILITY_MARKER = "r2-relay-leased-download-test-v1"
DOWNLOAD_CONFIG_TEXT = """schema_version = 1

# 由受控 GitHub Actions 管理；生产默认关闭。
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
test_expires_at_epoch = 0
"""

# 仅用于一次真实 R2 验收；固定资源上限，30 分钟后由 systemd timer 自动关闭。
DOWNLOAD_TEST_PROFILE_TEXT = """schema_version = 1

# REAL_R2_TEST_PROFILE_V1；禁止用于生产流量。
[download]
enabled = true
part_size_mib = 16
split_threshold_mib = 24
cache_ttl_seconds = 600
max_cache_bytes = 67108864
min_free_bytes = 5368709120
max_active_tasks = 1
max_retained_tasks = 2
max_waiters = 4
wait_timeout_seconds = 30
test_expires_at_epoch = {expires_at}
"""
DOWNLOAD_TEST_LEASE_SECONDS = 1800
DOWNLOAD_TEST_TIMER_INTERVAL_SECONDS = 30
DOWNLOAD_TEST_TIMER_PREFIX = "r2-relay-download-test-expiry-"
RECEIVER_BUILD_ID = "r2-relay-deploy-receiver-v7-leased-real-r2-test"
REQUIRED = {
    "vps/pyproject.toml",
    "vps/r2relay_vps/app.py",
    "deploy/vps.service",
    "deploy/requirements-vps.lock",
    ".deployment-commit",
}

PREVIOUS_UNIT_TEXT = """[Unit]
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

UNIT_TEXT = PREVIOUS_UNIT_TEXT.replace(
    "ReadWritePaths=/var/lib/r2-relay/cache",
    "ReadWritePaths=/var/lib/r2-relay/cache /var/lib/r2-relay/downloads",
)

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
            if "deploy/leased-download-test-v1" in names:
                test_marker = archive.extractfile(
                    archive.getmember("deploy/leased-download-test-v1")
                )
                if test_marker is None or test_marker.read(128).decode("ascii") != DOWNLOAD_TEST_CAPABILITY_MARKER + "\n":
                    raise DeployError("download_test_capability_marker_invalid")
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


def validate_host_state(*, require_active: bool = True) -> tuple[int, int, str]:
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
    if unit_text.strip() not in (
        LEGACY_UNIT_TEXT.strip(), PREVIOUS_UNIT_TEXT.strip(), UNIT_TEXT.strip()
    ):
        raise DeployError("installed_service_unit_not_recognized")
    active = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", SERVICE],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=10,
    )
    if require_active and active.returncode != 0:
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


def _ensure_download_cache_directory(uid: int, gid: int) -> None:
    parent = DOWNLOAD_CACHE.parent
    if parent.is_symlink() or not parent.is_dir():
        raise DeployError("download_cache_parent_missing_or_unsafe")
    parent_stat = parent.stat()
    if parent_stat.st_uid != 0 or parent_stat.st_mode & 0o022:
        raise DeployError("download_cache_parent_permissions_invalid")
    if DOWNLOAD_CACHE.is_symlink():
        raise DeployError("download_cache_directory_symlink")
    try:
        DOWNLOAD_CACHE.mkdir(mode=0o700, parents=False)
    except FileExistsError:
        pass
    else:
        os.chown(DOWNLOAD_CACHE, uid, gid)
        os.chmod(DOWNLOAD_CACHE, 0o700)
    info = DOWNLOAD_CACHE.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
            or info.st_gid != gid or stat.S_IMODE(info.st_mode) != 0o700):
        raise DeployError("download_cache_directory_permissions_invalid")


def _validate_download_cache_directory(uid: int, gid: int) -> None:
    if UNIT.is_symlink() or not UNIT.is_file():
        raise DeployError("download_cache_service_unit_missing_or_unsafe")
    if UNIT.read_text(encoding="utf-8").strip() != UNIT_TEXT.strip():
        raise DeployError("download_cache_systemd_write_path_not_prepared")
    if DOWNLOAD_CACHE.is_symlink() or not DOWNLOAD_CACHE.is_dir():
        raise DeployError("download_cache_directory_not_prepared")
    info = DOWNLOAD_CACHE.stat()
    if (info.st_uid != uid or info.st_gid != gid
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise DeployError("download_cache_directory_permissions_invalid")


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


def health_check(
    attempts: int = 25, *, expected_download_enabled: bool | None = None,
) -> bool:
    for _ in range(attempts):
        try:
            with urlopen("http://127.0.0.1:18085/healthz", timeout=2) as response:
                if response.status == 200:
                    payload = json.loads(response.read(1024).decode("utf-8"))
                    if (payload.get("status") == "ok"
                            and (expected_download_enabled is None
                                 or payload.get("download_enabled") is expected_download_enabled)):
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

        if _read_download_test_lease() is not None:
            raise DeployError("deployment_blocked_while_real_r2_test_lease_active")
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
            _ensure_download_cache_directory(uid, gid)
            write_atomic(UNIT, UNIT_TEXT)
            set_current(str(release))
            run_fixed(["/usr/bin/systemctl", "daemon-reload"], timeout=30)
            run_fixed(["/usr/bin/systemctl", "restart", SERVICE], timeout=90)
            expected_download_enabled = (
                False if (release / "deploy/download-config-dropin-v1").is_file() else None
            )
            if not health_check(expected_download_enabled=expected_download_enabled):
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


def parse_download_config_command(command: str | None) -> tuple[str, str]:
    if not isinstance(command, str):
        raise DeployError("missing_forced_command")
    match = re.fullmatch(r"download-config (disabled|real-test) ([0-9a-f]{40})", command)
    if not match:
        raise DeployError("download_config_command_not_allowed")
    return match.group(1), match.group(2)


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _validate_download_config_paths(gid: int) -> None:
    etc = DOWNLOAD_CONFIG_DIR.parent
    if etc.is_symlink() or not etc.is_dir():
        raise DeployError("download_config_etc_directory_missing_or_unsafe")
    etc_stat = etc.stat()
    if etc_stat.st_uid != 0 or etc_stat.st_mode & 0o022:
        raise DeployError("download_config_etc_directory_permissions_invalid")

    backup_parent = DOWNLOAD_CONFIG_BACKUPS.parent
    if backup_parent.is_symlink() or not backup_parent.is_dir():
        raise DeployError("download_config_backup_parent_missing_or_unsafe")
    backup_parent_stat = backup_parent.stat()
    if backup_parent_stat.st_uid != 0 or backup_parent_stat.st_mode & 0o022:
        raise DeployError("download_config_backup_parent_permissions_invalid")
    if DOWNLOAD_CONFIG_BACKUPS.is_symlink():
        raise DeployError("download_config_backup_root_symlink")

    parent = DOWNLOAD_CONFIG_DIR
    if parent.is_symlink():
        raise DeployError("download_config_parent_symlink")
    try:
        parent.mkdir(mode=0o710)
    except FileExistsError:
        pass
    except OSError:
        raise DeployError("download_config_parent_create_failed") from None
    else:
        os.chown(parent, 0, gid)
        os.chmod(parent, 0o710)

    if parent.is_symlink() or not parent.is_dir():
        raise DeployError("download_config_parent_missing_or_unsafe")
    parent_stat = parent.stat()
    if (parent_stat.st_uid != 0 or parent_stat.st_gid != gid
            or stat.S_IMODE(parent_stat.st_mode) != 0o710):
        raise DeployError("download_config_parent_permissions_invalid")

    if DOWNLOAD_CONFIG.is_symlink():
        raise DeployError("download_config_path_symlink")
    if DOWNLOAD_CONFIG.exists():
        info = DOWNLOAD_CONFIG.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_gid != gid or stat.S_IMODE(info.st_mode) != 0o640
                or info.st_nlink != 1 or info.st_size > 4096):
            raise DeployError("download_config_file_permissions_invalid")


def _atomic_write_download_config(
    content: bytes, gid: int, mode: int = 0o640,
) -> None:
    fd, name = tempfile.mkstemp(prefix=".r2-download-config-", dir=DOWNLOAD_CONFIG.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            os.fchown(output.fileno(), 0, gid)
            os.fchmod(output.fileno(), mode)
        os.replace(name, DOWNLOAD_CONFIG)
        _fsync_directory(DOWNLOAD_CONFIG.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _restore_download_config(
    previous: bytes | None, gid: int, previous_mode: int = 0o640,
    previous_gid: int | None = None,
) -> None:
    if previous is None:
        DOWNLOAD_CONFIG.unlink(missing_ok=True)
        _fsync_directory(DOWNLOAD_CONFIG.parent)
    else:
        _atomic_write_download_config(
            previous, gid if previous_gid is None else previous_gid, previous_mode
        )


def _write_download_config_backup(previous: bytes | None, previous_mode: int,
                                  previous_gid: int, sha: str, profile: str) -> Path:
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
        "previous_config_mode": oct(previous_mode) if previous is not None else None,
        "previous_config_gid": previous_gid if previous is not None else None,
        "config_path": str(DOWNLOAD_CONFIG),
        "profile": profile,
    }
    if previous is not None:
        previous_backup = backup / "download.toml"
        previous_backup.write_bytes(previous)
        with previous_backup.open("rb") as stream:
            os.fsync(stream.fileno())
        os.chown(previous_backup, 0, 0)
        os.chmod(previous_backup, 0o600)
    state_path = backup / "state.json"
    state_path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    with state_path.open("rb") as stream:
        os.fsync(stream.fileno())
    os.chown(state_path, 0, 0)
    os.chmod(state_path, 0o600)
    _fsync_directory(backup)
    _fsync_directory(DOWNLOAD_CONFIG_BACKUPS)
    return backup


def _ensure_download_test_state_dir() -> None:
    parent = DOWNLOAD_TEST_STATE_DIR.parent
    if parent.is_symlink() or not parent.is_dir():
        raise DeployError("download_test_state_parent_unsafe")
    parent_stat = parent.stat()
    if parent_stat.st_uid != 0 or parent_stat.st_mode & 0o022:
        raise DeployError("download_test_state_parent_permissions_invalid")
    if DOWNLOAD_TEST_STATE_DIR.is_symlink():
        raise DeployError("download_test_state_dir_symlink")
    try:
        DOWNLOAD_TEST_STATE_DIR.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = DOWNLOAD_TEST_STATE_DIR.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise DeployError("download_test_state_dir_permissions_invalid")


def _write_download_test_lease(lease: dict) -> None:
    _ensure_download_test_state_dir()
    fd, name = tempfile.mkstemp(prefix=".download-test-lease-", dir=DOWNLOAD_TEST_STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(lease, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), 0, 0)
            os.fchmod(stream.fileno(), 0o600)
        os.replace(name, DOWNLOAD_TEST_LEASE)
        _fsync_directory(DOWNLOAD_TEST_STATE_DIR)
    finally:
        Path(name).unlink(missing_ok=True)


def _read_download_test_lease() -> dict | None:
    if DOWNLOAD_TEST_LEASE.is_symlink():
        raise DeployError("download_test_lease_symlink")
    if not DOWNLOAD_TEST_LEASE.exists():
        return None
    info = DOWNLOAD_TEST_LEASE.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or info.st_size > 2048):
        raise DeployError("download_test_lease_permissions_invalid")
    try:
        value = json.loads(DOWNLOAD_TEST_LEASE.read_text(encoding="utf-8"))
    except Exception:
        raise DeployError("download_test_lease_invalid") from None
    if (not isinstance(value, dict)
            or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("lease_id", "")))
            or type(value.get("expires_at")) is not int
            or not SHA_RE.fullmatch(str(value.get("commit", "")))):
        raise DeployError("download_test_lease_invalid")
    return value


def _clear_download_test_lease() -> None:
    if DOWNLOAD_TEST_LEASE.is_symlink():
        raise DeployError("download_test_lease_symlink")
    DOWNLOAD_TEST_LEASE.unlink(missing_ok=True)
    if DOWNLOAD_TEST_STATE_DIR.exists():
        _fsync_directory(DOWNLOAD_TEST_STATE_DIR)


def _validate_download_test_systemd_dir() -> None:
    if SYSTEMD_UNIT_DIR.is_symlink() or not SYSTEMD_UNIT_DIR.is_dir():
        raise DeployError("download_test_systemd_unit_dir_unsafe")
    info = SYSTEMD_UNIT_DIR.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise DeployError("download_test_systemd_unit_dir_permissions_invalid")


def _download_test_timer_unit_name(lease_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", lease_id):
        raise DeployError("invalid_download_test_lease_id")
    return DOWNLOAD_TEST_TIMER_PREFIX + lease_id


def _schedule_download_test_expiry(lease_id: str) -> None:
    _validate_download_test_systemd_dir()
    unit = _download_test_timer_unit_name(lease_id)
    service_path = SYSTEMD_UNIT_DIR / (unit + ".service")
    timer_path = SYSTEMD_UNIT_DIR / (unit + ".timer")
    if service_path.exists() or timer_path.exists() or service_path.is_symlink() or timer_path.is_symlink():
        raise DeployError("download_test_timer_unit_already_exists")
    service_text = f"""[Unit]
Description=R2 Relay leased download test expiry ({lease_id})

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {RECEIVER_INSTALLED} --expire-download-test {lease_id}
"""
    timer_text = f"""[Unit]
Description=R2 Relay leased download test expiry timer ({lease_id})

[Timer]
OnBootSec={DOWNLOAD_TEST_TIMER_INTERVAL_SECONDS}s
OnUnitActiveSec={DOWNLOAD_TEST_TIMER_INTERVAL_SECONDS}s
AccuracySec=1s
Unit={unit}.service

[Install]
WantedBy=timers.target
"""
    try:
        write_atomic(service_path, service_text, mode=0o644)
        write_atomic(timer_path, timer_text, mode=0o644)
        run_fixed(["/usr/bin/systemctl", "daemon-reload"], timeout=30)
        run_fixed(["/usr/bin/systemctl", "enable", "--now", unit + ".timer"], timeout=30)
    except Exception:
        try:
            run_fixed(["/usr/bin/systemctl", "disable", "--now", unit + ".timer"], timeout=30)
        except Exception:
            pass
        for path in (timer_path, service_path):
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            except OSError:
                pass
        try:
            run_fixed(["/usr/bin/systemctl", "daemon-reload"], timeout=30)
        except Exception:
            pass
        raise


def _cleanup_download_test_expiry_timer(lease_id: str) -> None:
    _validate_download_test_systemd_dir()
    unit = _download_test_timer_unit_name(lease_id)
    service_path = SYSTEMD_UNIT_DIR / (unit + ".service")
    timer_path = SYSTEMD_UNIT_DIR / (unit + ".timer")
    try:
        run_fixed(["/usr/bin/systemctl", "disable", "--now", unit + ".timer"], timeout=30)
    except Exception:
        # It may already have fired or been manually disabled. Remove only the
        # nonce-specific unit files; a stale timer cannot affect a newer lease.
        pass
    _validate_download_test_systemd_dir()
    for path in (timer_path, service_path):
        if path.is_symlink():
            raise DeployError("download_test_timer_unit_symlink")
        if path.exists():
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or stat.S_IMODE(info.st_mode) != 0o644 or info.st_nlink != 1):
                raise DeployError("download_test_timer_unit_permissions_invalid")
            path.unlink()
    _fsync_directory(SYSTEMD_UNIT_DIR)
    try:
        run_fixed(["/usr/bin/systemctl", "daemon-reload"], timeout=30)
    except Exception:
        pass


def apply_download_config_real_test(sha: str) -> dict:
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

        uid, gid, current_target = validate_host_state()
        if not current_target or Path(current_target).name != sha:
            raise DeployError("download_test_target_must_be_active_release")
        release_marker = Path(current_target) / "deploy/download-config-dropin-v1"
        if (not release_marker.is_file()
                or release_marker.read_text(encoding="ascii").strip() != DOWNLOAD_CONFIG_MARKER):
            raise DeployError("download_config_capability_marker_missing")
        test_marker = Path(current_target) / "deploy/leased-download-test-v1"
        if (not test_marker.is_file()
                or test_marker.read_text(encoding="ascii").strip() != DOWNLOAD_TEST_CAPABILITY_MARKER):
            raise DeployError("download_test_capability_marker_missing")

        _validate_download_config_paths(gid)
        _validate_download_cache_directory(uid, gid)
        current = DOWNLOAD_CONFIG.read_bytes() if DOWNLOAD_CONFIG.exists() else None
        disabled = DOWNLOAD_CONFIG_TEXT.encode("utf-8")
        if current != disabled:
            raise DeployError("download_test_requires_fixed_disabled_profile")
        if not health_check(expected_download_enabled=False):
            raise DeployError("download_test_requires_healthy_disabled_service")
        _ensure_download_test_state_dir()
        if _read_download_test_lease() is not None:
            raise DeployError("download_test_lease_already_exists")

        backup = _write_download_config_backup(
            current, 0o640, gid, sha, "real-r2-test-v1"
        )
        lease_id = secrets.token_hex(16)
        expires_at = int(time.time()) + DOWNLOAD_TEST_LEASE_SECONDS
        lease = {"lease_id": lease_id, "expires_at": expires_at, "commit": sha}
        try:
            _write_download_test_lease(lease)
            test_profile = DOWNLOAD_TEST_PROFILE_TEXT.format(expires_at=expires_at)
            _atomic_write_download_config(test_profile.encode("utf-8"), gid)
            _schedule_download_test_expiry(lease_id)
            run_fixed(["/usr/bin/systemctl", "restart", SERVICE], timeout=90)
            if not health_check(expected_download_enabled=True):
                raise DeployError("download_test_enable_health_check_failed")
            return {
                "status": "DOWNLOAD_TEST_ENABLED",
                "commit": sha,
                "download_enabled": True,
                "profile": "real-r2-test-v1",
                "max_cache_bytes": 67108864,
                "min_free_bytes": 5368709120,
                "max_active_tasks": 1,
                "max_retained_tasks": 2,
                "max_waiters": 4,
                "expires_at": expires_at,
                "lease_minutes": 30,
                "config_path": str(DOWNLOAD_CONFIG),
                "config_mode": "0640",
                "backup": str(backup),
                "service_restarted": True,
            }
        except Exception as exc:
            try:
                _atomic_write_download_config(disabled, gid)
                _clear_download_test_lease()
                try:
                    _cleanup_download_test_expiry_timer(lease_id)
                except Exception:
                    pass
                run_fixed(["/usr/bin/systemctl", "restart", SERVICE], timeout=90)
                if not health_check(expected_download_enabled=False):
                    raise DeployError("download_test_disable_after_failure_health_check_failed")
            except Exception:
                try:
                    run_fixed(["/usr/bin/systemctl", "stop", SERVICE], timeout=30)
                except Exception:
                    pass
                raise DeployError(
                    "download_test_enable_failed_disable_failed:" + type(exc).__name__
                ) from None
            raise DeployError(
                "download_test_enable_failed_disabled:" + type(exc).__name__
            ) from None


def _force_download_test_disabled(gid: int, lease_id: str | None = None) -> None:
    _validate_download_config_paths(gid)
    _atomic_write_download_config(DOWNLOAD_CONFIG_TEXT.encode("utf-8"), gid)
    _clear_download_test_lease()
    run_fixed(["/usr/bin/systemctl", "restart", SERVICE], timeout=90)
    if not health_check(expected_download_enabled=False):
        raise DeployError("download_test_disable_health_check_failed")
    if lease_id is not None:
        try:
            _cleanup_download_test_expiry_timer(lease_id)
        except Exception:
            # The nonce-bound timer will see a stale lease and clean itself.
            pass


def _force_download_test_disabled_or_stop(lease_id: str) -> None:
    try:
        _uid, gid, _target = validate_host_state(require_active=False)
        _force_download_test_disabled(gid, lease_id)
    except Exception:
        try:
            run_fixed(["/usr/bin/systemctl", "stop", SERVICE], timeout=30)
        except Exception:
            pass
        raise DeployError("download_test_disable_failed_fail_closed") from None


def expire_download_test(lease_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", lease_id):
        raise DeployError("invalid_download_test_lease_id")
    if os.geteuid() != 0:
        raise DeployError("root_required")
    lock_fd = open_deploy_lock()
    with os.fdopen(lock_fd, "r+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError("deployment_already_running") from None
        try:
            lease = _read_download_test_lease()
        except DeployError:
            _force_download_test_disabled_or_stop(lease_id)
            return {
                "status": "DOWNLOAD_TEST_AUTO_DISABLED",
                "download_enabled": False,
                "reason": "invalid_lease_fail_closed",
                "service_restarted": True,
            }
        if lease is None or lease["lease_id"] != lease_id:
            try:
                _cleanup_download_test_expiry_timer(lease_id)
            except Exception:
                pass
            return {"status": "DOWNLOAD_TEST_EXPIRY_STALE", "download_enabled": None}
        if lease["expires_at"] > int(time.time()):
            return {
                "status": "DOWNLOAD_TEST_EXPIRY_NOT_DUE",
                "expires_at": lease["expires_at"],
            }
        try:
            _uid, gid, _current_target = validate_host_state(require_active=False)
            _force_download_test_disabled(gid, lease_id)
        except Exception as exc:
            try:
                run_fixed(["/usr/bin/systemctl", "stop", SERVICE], timeout=30)
            except Exception:
                pass
            raise DeployError(
                "download_test_expiry_disable_failed_fail_closed:" + type(exc).__name__
            ) from None
        return {
            "status": "DOWNLOAD_TEST_AUTO_DISABLED",
            "commit": lease["commit"],
            "download_enabled": False,
            "config_path": str(DOWNLOAD_CONFIG),
            "service_restarted": True,
        }


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

        try:
            _uid, gid, _previous_target = validate_host_state(require_active=False)
            _validate_download_config_paths(gid)
        except Exception:
            try:
                run_fixed(["/usr/bin/systemctl", "stop", SERVICE], timeout=30)
            except Exception:
                pass
            raise
        expected = DOWNLOAD_CONFIG_TEXT.encode("utf-8")
        previous_info = DOWNLOAD_CONFIG.stat() if DOWNLOAD_CONFIG.exists() else None
        previous = DOWNLOAD_CONFIG.read_bytes() if previous_info is not None else None
        previous_mode = stat.S_IMODE(previous_info.st_mode) if previous_info is not None else 0o640
        previous_gid = previous_info.st_gid if previous_info is not None else gid

        if previous == expected:
            try:
                existing_lease = _read_download_test_lease()
            except DeployError:
                existing_lease = None
            _clear_download_test_lease()
            if existing_lease is not None:
                try:
                    _cleanup_download_test_expiry_timer(existing_lease["lease_id"])
                except Exception:
                    pass
            if not health_check(expected_download_enabled=False):
                try:
                    run_fixed(["/usr/bin/systemctl", "restart", SERVICE], timeout=90)
                    if not health_check(expected_download_enabled=False):
                        raise DeployError("download_config_existing_health_check_failed")
                    restarted = True
                except Exception:
                    try:
                        run_fixed(["/usr/bin/systemctl", "stop", SERVICE], timeout=30)
                    except Exception:
                        pass
                    raise DeployError("download_config_existing_health_check_failed_service_stopped") from None
            else:
                restarted = False
            return {
                "status": "DOWNLOAD_CONFIG_ALREADY_APPLIED",
                "commit": sha,
                "download_enabled": False,
                "config_path": str(DOWNLOAD_CONFIG),
                "service_restarted": restarted,
            }

        backup = _write_download_config_backup(
            previous, previous_mode, previous_gid, sha, "disabled"
        )
        try:
            # Disabling is fail-closed: never restore a prior enabled profile.
            _atomic_write_download_config(expected, gid)
            try:
                existing_lease = _read_download_test_lease()
            except DeployError:
                existing_lease = None
            _clear_download_test_lease()
            if existing_lease is not None:
                try:
                    _cleanup_download_test_expiry_timer(existing_lease["lease_id"])
                except Exception:
                    pass
            run_fixed(["/usr/bin/systemctl", "restart", SERVICE], timeout=90)
            if not health_check(expected_download_enabled=False):
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
                _atomic_write_download_config(expected, gid)
                _clear_download_test_lease()
                run_fixed(["/usr/bin/systemctl", "restart", SERVICE], timeout=90)
                if not health_check(expected_download_enabled=False):
                    raise DeployError("download_config_disable_health_check_failed")
            except Exception:
                # A disabled file remains on disk; stopping the service prevents
                # an old enabled process from continuing if restart/health failed.
                try:
                    run_fixed(["/usr/bin/systemctl", "stop", SERVICE], timeout=30)
                except Exception:
                    pass
                raise DeployError(
                    "download_config_disable_failed_fail_closed:" + type(exc).__name__
                ) from None
            raise DeployError(
                "download_config_disable_failed_but_disabled:" + type(exc).__name__
            ) from None


def main() -> int:
    try:
        original_command = os.environ.get("SSH_ORIGINAL_COMMAND")
        if (not original_command and len(sys.argv) == 3
                and sys.argv[1] == "--expire-download-test"):
            result = expire_download_test(sys.argv[2])
            emit(result.pop("status"), **result)
            return 0

        if isinstance(original_command, str) and original_command.startswith("download-config"):
            mode, sha = parse_download_config_command(original_command)
            if sys.stdin.buffer.read(1):
                raise DeployError("download_config_stdin_must_be_empty")
            if mode == "disabled":
                result = apply_download_config_disabled(sha)
            else:
                result = apply_download_config_real_test(sha)
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

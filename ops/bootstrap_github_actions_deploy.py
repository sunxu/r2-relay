#!/usr/bin/env python3
"""One-time root bootstrap for the dedicated GitHub Actions deploy account.

Run as root on the VPS after generating an Ed25519 key pair and storing the
private key in the GitHub Actions secret VPS_DEPLOY_SSH_KEY.
"""
from __future__ import annotations

import argparse
import base64
import grp
import hashlib
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import time

DEPLOY_USER = "r2relay-deploy"
DEPLOY_HOME = Path("/var/lib/r2relay-deploy")
SSH_DIR = DEPLOY_HOME / ".ssh"
AUTHORIZED_KEYS = SSH_DIR / "authorized_keys"
RECEIVER_SOURCE = Path(__file__).with_name("r2relay_deploy_receiver.py")
RECEIVER_DEST = Path("/usr/local/libexec/r2relay_deploy_receiver.py")
SUDOERS = Path("/etc/sudoers.d/r2-relay-deploy")
BACKUPS = Path("/var/backups/r2-relay/deploy-bootstrap")
SUDO_COMMAND = "/usr/local/libexec/r2relay_deploy_receiver.py"
FORCED_COMMAND = "/usr/bin/sudo -n " + SUDO_COMMAND
SUDOERS_TEXT = (
    'Defaults:r2relay-deploy env_keep += "SSH_ORIGINAL_COMMAND"\n'
    'r2relay-deploy ALL=(root) NOPASSWD: /usr/local/libexec/r2relay_deploy_receiver.py ""\n'
)
LEGACY_SUDOERS_TEXT = (
    'Defaults:r2relay-deploy env_keep += "SSH_ORIGINAL_COMMAND"\n'
    'r2relay-deploy ALL=(root) NOPASSWD: /usr/bin/python3 /usr/local/libexec/r2relay_deploy_receiver.py ""\n'
)


def is_managed_sudoers_policy(content: str) -> bool:
    return content in {SUDOERS_TEXT, LEGACY_SUDOERS_TEXT}
KEY_RE = re.compile(r"^ssh-ed25519 ([A-Za-z0-9+/]+={0,2})(?: (.*))?$")


class BootstrapError(Exception):
    pass


def parse_public_key(value: str) -> tuple[str, str, str]:
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise BootstrapError("invalid_public_key")
    match = KEY_RE.fullmatch(value.strip())
    if not match:
        raise BootstrapError("ed25519_public_key_required")
    encoded, comment = match.group(1), match.group(2) or "r2-relay-github-actions"
    try:
        blob = base64.b64decode(encoded, validate=True)
    except Exception:
        raise BootstrapError("invalid_public_key_encoding") from None
    if (not blob.startswith(b"\x00\x00\x00\x0bssh-ed25519")
            or blob[15:19] != b"\x00\x00\x00 " or len(blob) != 51):
        raise BootstrapError("invalid_ed25519_key_blob")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    comment = re.sub(r"[\x00-\x1f\x7f\"]", "", comment)[:80]
    return encoded, comment or "r2-relay-github-actions", fingerprint


def existing_key_data(lines: list[str]) -> set[str]:
    found = set()
    for line in lines:
        tokens = line.strip().split()
        for index, token in enumerate(tokens[:-1]):
            if token == "ssh-ed25519":
                found.add(tokens[index + 1])
                break
    return found


def authorized_key_line(encoded: str, comment: str) -> str:
    return f'command="{FORCED_COMMAND}",restrict ssh-ed25519 {encoded} {comment}'


def atomic_write(path: Path, content: bytes, mode: int, uid: int = 0, gid: int = 0) -> None:
    fd, name = tempfile.mkstemp(prefix=".r2-deploy-bootstrap-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chown(name, uid, gid)
        os.chmod(name, mode)
        os.replace(name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        Path(name).unlink(missing_ok=True)


def run_checked(argv: list[str], failure: str) -> None:
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, check=False, timeout=30)
    if result.returncode != 0:
        raise BootstrapError(failure)


def snapshot(path: Path):
    if not path.exists():
        return None
    info = path.stat()
    return (path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)


def password_is_locked(username: str, shadow_path: Path = Path("/etc/shadow")) -> bool:
    """Return only the lock state; never expose or log password hashes."""
    try:
        for line in shadow_path.read_text(encoding="utf-8").splitlines():
            fields = line.split(":")
            if fields and fields[0] == username:
                password_field = fields[1] if len(fields) > 1 else ""
                return password_field.startswith(("!", "*"))
    except OSError:
        raise BootstrapError("shadow_lock_state_unavailable") from None
    raise BootstrapError("shadow_account_missing")


def restore_snapshot(path: Path, value) -> None:
    if value is None:
        path.unlink(missing_ok=True)
        return
    content, mode, uid, gid = value
    atomic_write(path, content, mode, uid, gid)


def bootstrap(public_key: str, *, apply: bool) -> dict:
    if os.geteuid() != 0:
        raise BootstrapError("root_required")
    if not RECEIVER_SOURCE.is_file() or RECEIVER_SOURCE.is_symlink():
        raise BootstrapError("receiver_source_missing_or_unsafe")
    receiver_text = RECEIVER_SOURCE.read_text(encoding="utf-8")
    compile(receiver_text, str(RECEIVER_SOURCE), "exec")
    encoded, comment, fingerprint = parse_public_key(public_key)

    if not Path("/usr/bin/sudo").is_file() or not Path("/usr/sbin/visudo").is_file():
        raise BootstrapError("sudo_and_visudo_required")
    account = None
    try:
        account = pwd.getpwnam(DEPLOY_USER)
    except KeyError:
        pass
    if account is not None and (account.pw_dir != str(DEPLOY_HOME) or account.pw_shell != "/bin/sh"):
        raise BootstrapError("deploy_account_conflicts_with_existing_user")
    if account is not None and set(os.getgrouplist(DEPLOY_USER, account.pw_gid)) != {account.pw_gid}:
        raise BootstrapError("deploy_account_has_unexpected_supplementary_groups")
    if account is not None and not password_is_locked(DEPLOY_USER):
        raise BootstrapError("existing_deploy_account_password_not_locked")
    if account is None and (DEPLOY_HOME.exists() or SSH_DIR.exists() or AUTHORIZED_KEYS.exists()):
        raise BootstrapError("deploy_home_preexisting_without_account")
    if DEPLOY_HOME.is_symlink() or (DEPLOY_HOME.exists() and not DEPLOY_HOME.is_dir()):
        raise BootstrapError("deploy_home_unsafe")
    if SSH_DIR.is_symlink() or (SSH_DIR.exists() and not SSH_DIR.is_dir()):
        raise BootstrapError("ssh_directory_unsafe")
    if AUTHORIZED_KEYS.is_symlink():
        raise BootstrapError("authorized_keys_symlink")
    if SUDOERS.is_symlink():
        raise BootstrapError("sudoers_symlink")
    if SUDOERS.exists():
        info = SUDOERS.stat()
        if not SUDOERS.is_file() or info.st_uid != 0 or info.st_mode & 0o022:
            raise BootstrapError("sudoers_permissions_invalid")
        if not is_managed_sudoers_policy(SUDOERS.read_text(encoding="utf-8")):
            raise BootstrapError("sudoers_file_conflict")
    if RECEIVER_DEST.is_symlink():
        raise BootstrapError("receiver_destination_symlink")
    if RECEIVER_DEST.exists() and RECEIVER_DEST.stat().st_uid != 0:
        raise BootstrapError("receiver_destination_not_root_owned")
    if RECEIVER_DEST.parent.is_symlink():
        raise BootstrapError("receiver_parent_symlink")
    sudoers_dir = Path("/etc/sudoers.d")
    if sudoers_dir.is_symlink() or not sudoers_dir.is_dir():
        raise BootstrapError("sudoers_directory_unsafe")
    sudoers_dir_stat = sudoers_dir.stat()
    if sudoers_dir_stat.st_uid != 0 or sudoers_dir_stat.st_mode & 0o022:
        raise BootstrapError("sudoers_directory_permissions_invalid")
    if RECEIVER_DEST.parent.exists():
        receiver_parent_stat = RECEIVER_DEST.parent.stat()
        if not RECEIVER_DEST.parent.is_dir() or receiver_parent_stat.st_uid != 0 or receiver_parent_stat.st_mode & 0o022:
            raise BootstrapError("receiver_parent_permissions_invalid")

    old_keys = AUTHORIZED_KEYS.read_bytes() if AUTHORIZED_KEYS.exists() else b""
    try:
        old_lines = old_keys.decode("utf-8").splitlines()
    except UnicodeError:
        raise BootstrapError("authorized_keys_encoding_invalid") from None
    key_exists = encoded in existing_key_data(old_lines)
    expected_line = authorized_key_line(encoded, comment)
    configured_lines = [line.strip() for line in old_lines
                        if line.strip() and not line.lstrip().startswith("#")]
    if any(line != expected_line for line in configured_lines):
        raise BootstrapError("unexpected_deploy_account_authorized_key")
    if key_exists and expected_line not in configured_lines:
        raise BootstrapError("key_already_exists_with_different_options")
    new_keys = old_keys
    if not key_exists:
        new_keys = (old_keys.decode("utf-8").rstrip("\n") + ("\n" if old_keys else "") + expected_line + "\n").encode()

    if not apply:
        return {"status": "DRY_RUN_PASS", "fingerprint": fingerprint,
                "deploy_account": DEPLOY_USER, "account_would_be_created": account is None,
                "authorized_key_added": not key_exists, "forced_command": FORCED_COMMAND,
                "sudo_scope": SUDO_COMMAND, "pty_disabled": True,
                "port_forwarding_disabled": True, "receiver_destination": str(RECEIVER_DEST)}

    if BACKUPS.is_symlink():
        raise BootstrapError("backup_directory_symlink")
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_info = BACKUPS.stat()
    if backup_info.st_uid != 0 or backup_info.st_mode & 0o077:
        raise BootstrapError("backup_directory_permissions_invalid")
    os.chmod(BACKUPS, 0o700)
    safe_fingerprint = fingerprint.replace("SHA256:", "").replace("/", "_").replace("+", "-").replace("=", "")
    backup = BACKUPS / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + safe_fingerprint[-12:])
    backup.mkdir(mode=0o700)

    created_account = account is None
    snapshots = {path: snapshot(path) for path in (AUTHORIZED_KEYS, SUDOERS, RECEIVER_DEST)}
    try:
        if created_account:
            run_checked(["/usr/sbin/useradd", "--system", "--create-home",
                         "--home-dir", str(DEPLOY_HOME), "--shell", "/bin/sh",
                         "--user-group", DEPLOY_USER], "deploy_account_creation_failed")
        account = pwd.getpwnam(DEPLOY_USER)
        group = grp.getgrgid(account.pw_gid)
        if set(os.getgrouplist(DEPLOY_USER, account.pw_gid)) != {account.pw_gid}:
            raise BootstrapError("deploy_account_has_unexpected_supplementary_groups")
        if created_account:
            run_checked(["/usr/sbin/usermod", "--lock", DEPLOY_USER], "deploy_account_password_lock_failed")
        DEPLOY_HOME.mkdir(mode=0o750, parents=True, exist_ok=True)
        if DEPLOY_HOME.is_symlink():
            raise BootstrapError("deploy_home_symlink")
        os.chown(DEPLOY_HOME, account.pw_uid, group.gr_gid)
        os.chmod(DEPLOY_HOME, 0o750)
        SSH_DIR.mkdir(mode=0o700, exist_ok=True)
        os.chown(SSH_DIR, account.pw_uid, group.gr_gid)
        os.chmod(SSH_DIR, 0o700)
        if AUTHORIZED_KEYS.exists():
            key_info = AUTHORIZED_KEYS.stat()
            if not AUTHORIZED_KEYS.is_file() or key_info.st_uid != account.pw_uid or key_info.st_mode & 0o022:
                raise BootstrapError("authorized_keys_permissions_invalid")
            shutil.copy2(AUTHORIZED_KEYS, backup / "authorized_keys")
        if SUDOERS.exists():
            shutil.copy2(SUDOERS, backup / "sudoers")
        if RECEIVER_DEST.exists():
            shutil.copy2(RECEIVER_DEST, backup / "r2relay_deploy_receiver.py")
        RECEIVER_DEST.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        atomic_write(RECEIVER_DEST, receiver_text.encode(), 0o755, 0, 0)
        atomic_write(SUDOERS, SUDOERS_TEXT.encode(), 0o440, 0, 0)
        atomic_write(AUTHORIZED_KEYS, new_keys, 0o600, account.pw_uid, group.gr_gid)
        run_checked(["/usr/sbin/visudo", "-cf", str(SUDOERS)], "sudoers_validation_failed")
        return {"status": "BOOTSTRAP_APPLIED", "fingerprint": fingerprint,
                "deploy_account": DEPLOY_USER, "authorized_key_added": not key_exists,
                "forced_command": FORCED_COMMAND, "sudo_scope": SUDO_COMMAND,
                "pty_disabled": True, "port_forwarding_disabled": True,
                "receiver": str(RECEIVER_DEST), "sudoers": str(SUDOERS),
                "backup": str(backup), "sshd_restarted": False}
    except Exception:
        for path, value in snapshots.items():
            try:
                restore_snapshot(path, value)
            except Exception:
                pass
        if created_account:
            subprocess.run(["/usr/sbin/userdel", "--remove", DEPLOY_USER],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False, timeout=30)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="create the dedicated deploy account and install its restricted SSH key")
    args = parser.parse_args()
    try:
        import json
        print(json.dumps(bootstrap(args.public_key, apply=args.apply), sort_keys=True))
        return 0
    except (BootstrapError, OSError, ValueError, subprocess.SubprocessError) as exc:
        import json
        print(json.dumps({"status": "FAILED", "reason": str(exc)[:120]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
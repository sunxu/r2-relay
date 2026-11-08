#!/usr/bin/env python3
"""One-time root bootstrap for the GitHub Actions forced-command deploy key.

Run as root on the VPS after generating an Ed25519 key pair and storing the
private key in the GitHub Actions secret VPS_DEPLOY_SSH_KEY.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time

SSH_DIR = Path("/root/.ssh")
AUTHORIZED_KEYS = SSH_DIR / "authorized_keys"
RECEIVER_SOURCE = Path(__file__).with_name("r2relay_deploy_receiver.py")
RECEIVER_DEST = Path("/usr/local/libexec/r2relay_deploy_receiver.py")
BACKUPS = Path("/var/backups/r2-relay/deploy-bootstrap")
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
    if not blob.startswith(b"\x00\x00\x00\x0bssh-ed25519") or blob[15:19] != b"\x00\x00\x00 " or len(blob) != 51:
        raise BootstrapError("invalid_ed25519_key_blob")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return encoded, comment[:80].replace('"', ""), fingerprint


def existing_key_data(lines: list[str]) -> set[str]:
    found = set()
    for line in lines:
        tokens = line.strip().split()
        for index, token in enumerate(tokens[:-1]):
            if token == "ssh-ed25519":
                found.add(tokens[index + 1])
                break
    return found


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    fd, name = tempfile.mkstemp(prefix=".github-deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chown(name, 0, 0)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def bootstrap(public_key: str, *, apply: bool) -> dict:
    if os.geteuid() != 0:
        raise BootstrapError("root_required")
    if not RECEIVER_SOURCE.is_file() or RECEIVER_SOURCE.is_symlink():
        raise BootstrapError("receiver_source_missing_or_unsafe")
    receiver_text = RECEIVER_SOURCE.read_text(encoding="utf-8")
    compile(receiver_text, str(RECEIVER_SOURCE), "exec")
    encoded, comment, fingerprint = parse_public_key(public_key)

    if SSH_DIR.is_symlink():
        raise BootstrapError("ssh_directory_symlink")
    if not SSH_DIR.exists():
        if not apply:
            return {"status": "DRY_RUN_PASS", "would_create_ssh_dir": True,
                    "fingerprint": fingerprint, "forced_command": True}
        SSH_DIR.mkdir(mode=0o700)
    info = SSH_DIR.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise BootstrapError("ssh_directory_permissions_invalid")
    if AUTHORIZED_KEYS.is_symlink():
        raise BootstrapError("authorized_keys_symlink")
    if AUTHORIZED_KEYS.exists():
        key_stat = AUTHORIZED_KEYS.stat()
        if not AUTHORIZED_KEYS.is_file() or key_stat.st_uid != 0 or key_stat.st_mode & 0o022:
            raise BootstrapError("authorized_keys_permissions_invalid")
    old_keys = AUTHORIZED_KEYS.read_bytes() if AUTHORIZED_KEYS.exists() else b""
    old_lines = old_keys.decode("utf-8").splitlines()
    if encoded in existing_key_data(old_lines):
        for line in old_lines:
            if encoded in line and "r2relay_deploy_receiver.py" in line and "restrict" in line:
                return {"status": "ALREADY_CONFIGURED", "fingerprint": fingerprint}
        raise BootstrapError("key_already_exists_with_different_options")

    line = (
        'command="/usr/bin/python3 /usr/local/libexec/r2relay_deploy_receiver.py",'
        f'restrict ssh-ed25519 {encoded} {comment}'
    )
    new_keys = (old_keys.decode("utf-8").rstrip("\n") + ("\n" if old_keys else "") + line + "\n").encode()
    if not apply:
        return {"status": "DRY_RUN_PASS", "fingerprint": fingerprint,
                "authorized_key_added": True, "forced_command": True,
                "pty_disabled": True, "port_forwarding_disabled": True,
                "receiver_destination": str(RECEIVER_DEST)}

    if RECEIVER_DEST.parent.is_symlink():
        raise BootstrapError("receiver_parent_symlink")
    if RECEIVER_DEST.parent.exists() and not RECEIVER_DEST.parent.is_dir():
        raise BootstrapError("receiver_parent_not_directory")
    if RECEIVER_DEST.is_symlink():
        raise BootstrapError("receiver_destination_symlink")
    if RECEIVER_DEST.exists() and RECEIVER_DEST.stat().st_uid != 0:
        raise BootstrapError("receiver_destination_not_root_owned")
    if BACKUPS.is_symlink():
        raise BootstrapError("backup_directory_symlink")
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(BACKUPS, 0o700)
    safe_fingerprint = fingerprint.replace("SHA256:", "").replace("/", "_").replace("+", "-").replace("=", "")
    backup = BACKUPS / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + safe_fingerprint[-12:])
    backup.mkdir(mode=0o700)
    if AUTHORIZED_KEYS.exists():
        shutil.copy2(AUTHORIZED_KEYS, backup / "authorized_keys")
    if RECEIVER_DEST.exists():
        shutil.copy2(RECEIVER_DEST, backup / "r2relay_deploy_receiver.py")
    try:
        RECEIVER_DEST.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        atomic_write(RECEIVER_DEST, receiver_text.encode(), 0o755)
        atomic_write(AUTHORIZED_KEYS, new_keys, 0o600)
        os.chown(SSH_DIR, 0, 0)
        os.chmod(SSH_DIR, 0o700)
        return {"status": "BOOTSTRAP_APPLIED", "fingerprint": fingerprint,
                "authorized_key_added": True, "forced_command": True,
                "pty_disabled": True, "port_forwarding_disabled": True,
                "receiver": str(RECEIVER_DEST), "backup": str(backup),
                "sshd_restarted": False}
    except Exception:
        if AUTHORIZED_KEYS.exists():
            if old_keys:
                atomic_write(AUTHORIZED_KEYS, old_keys, 0o600)
            else:
                AUTHORIZED_KEYS.unlink(missing_ok=True)
        if (backup / "r2relay_deploy_receiver.py").exists():
            shutil.copy2(backup / "r2relay_deploy_receiver.py", RECEIVER_DEST)
        else:
            RECEIVER_DEST.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="write the forced-command key and root receiver")
    args = parser.parse_args()
    try:
        print(__import__("json").dumps(bootstrap(args.public_key, apply=args.apply),
                                       sort_keys=True))
        return 0
    except (BootstrapError, OSError, ValueError) as exc:
        print(__import__("json").dumps({"status": "FAILED", "reason": str(exc)[:120]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

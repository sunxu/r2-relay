#!/usr/bin/env python3
"""Prepare fixed R2 Relay systemd files; never start/enable service or emit token."""
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess

APP = Path("/opt/r2-relay")
UNIT_SOURCE = APP / "deploy/vps.service"
ENV_SOURCE = APP / "deploy/vps.env.example"
ENV_DIRECTORY = Path("/etc/r2-relay")
ENV_DEST = ENV_DIRECTORY / "vps.env"
UNIT_DEST = Path("/etc/systemd/system/r2-relay-vps.service")
REQUIRED = ("User=r2relay", "Group=r2relay",
            "EnvironmentFile=/etc/r2-relay/vps.env",
            "--host 127.0.0.1", "--port 18085",
            "ProtectSystem=strict", "NoNewPrivileges=true")

def check():
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    if not APP.is_dir() or APP.is_symlink():
        raise RuntimeError("application_missing")
    if not (APP / ".venv/bin/uvicorn").is_file():
        raise RuntimeError("uvicorn_missing")
    if UNIT_DEST.exists() or UNIT_DEST.is_symlink():
        raise RuntimeError("unit_already_exists")
    if ENV_DEST.exists() or ENV_DEST.is_symlink():
        raise RuntimeError("environment_already_exists")
    if ENV_DIRECTORY.is_symlink():
        raise RuntimeError("environment_directory_symlink")
    text = UNIT_SOURCE.read_text(encoding="utf-8")
    if not all(required in text for required in REQUIRED):
        raise RuntimeError("unit_template_rejected")
    if "ExecStart=/opt/r2-relay/.venv/bin/uvicorn" not in text:
        raise RuntimeError("unit_exec_rejected")
    if "REPLACE_WITH_LONG_RANDOM_SECRET" not in ENV_SOURCE.read_text(encoding="utf-8"):
        raise RuntimeError("environment_template_rejected")
    return text

def prepare():
    unit = check()
    env_created = False
    unit_created = False
    try:
        ENV_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ENV_DIRECTORY, 0o700)
        token = secrets.token_urlsafe(48)
        content = ENV_SOURCE.read_text(encoding="utf-8").replace(
            "REPLACE_WITH_LONG_RANDOM_SECRET", token)
        fd = os.open(ENV_DEST, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        env_created = True
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
        os.chmod(ENV_DEST, 0o600)
        # Fixed file only, no path parameter from MCP.
        fd = os.open(UNIT_DEST, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
        unit_created = True
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(unit)
        os.chmod(UNIT_DEST, 0o644)
        p = subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20)
        if p.returncode:
            raise RuntimeError("daemon_reload_failed")
        return {"status": "SERVICE_PREPARED_NOT_STARTED",
                "unit_created": True, "env_created": True,
                "env_mode": "0600", "token_emitted": False,
                "service_started": False, "service_enabled": False,
                "port_443_modified": False}
    except Exception:
        if unit_created:
            UNIT_DEST.unlink(missing_ok=True)
        if env_created:
            ENV_DEST.unlink(missing_ok=True)
        raise

if __name__ == "__main__":
    try:
        result = prepare()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result = {"status": "SERVICE_PREPARE_FAILED", "error_type": type(exc).__name__,
                  "secrets_emitted": False}
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "SERVICE_PREPARED_NOT_STARTED" else 1)

#!/usr/bin/env python3
"""Install audited generic MCP tools into the existing FastMCP service.

Must run via root SSH as a one-time bootstrap. Does not alter sing-box/443.
"""
from __future__ import annotations
import argparse
import ast
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

SERVER = Path("/opt/r2-mcp/server.py")
MODULE = Path("/opt/r2-mcp/generic_vps_mcp.py")
WORKSPACE = Path("/var/lib/vps-mcp/workspaces")
DROPIN = Path("/etc/systemd/system/r2-mcp.service.d/generic-workspace.conf")
BACKUPS = Path("/var/backups/vps-mcp")
INSERT = (
    "from generic_vps_mcp import register as register_generic_vps\n"
    "register_generic_vps(mcp)\n"
)

def add_registration(source: str) -> str:
    """Use Python AST to locate a unique top-level FastMCP assignment."""
    if "register_generic_vps(mcp)" in source:
        return source
    tree = ast.parse(source)
    candidates = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "mcp" for t in node.targets
        ):
            if isinstance(node.value, ast.Call) and (
                isinstance(node.value.func, ast.Name)
                and node.value.func.id == "FastMCP"
            ):
                candidates.append(node.end_lineno)
    if len(candidates) != 1:
        raise ValueError("expected exactly one top-level mcp = FastMCP(...)")
    lines = source.splitlines(keepends=True)
    lines.insert(candidates[0], "\n" + INSERT)
    result = "".join(lines)
    ast.parse(result)
    return result

def invoke(argv: list[str]) -> None:
    subprocess.run(argv, check=True, timeout=25, shell=False)

def install(source_file: Path) -> dict:
    if os.geteuid() != 0:
        raise PermissionError("root required for one-time bootstrap")
    if not SERVER.is_file() or not source_file.is_file():
        raise FileNotFoundError("server or source module missing")
    if MODULE.exists() and MODULE.is_symlink():
        raise ValueError("existing MCP module symlink denied")
    for parent in (WORKSPACE.parent, WORKSPACE):
        if parent.is_symlink():
            raise ValueError("workspace symlink denied")
    original = SERVER.read_text(encoding="utf-8")
    updated = add_registration(original)
    module_text = source_file.read_text(encoding="utf-8")
    compile(module_text, str(MODULE), "exec")
    compile(updated, str(SERVER), "exec")
    if MODULE.exists():
        raise FileExistsError("module exists; do not overwrite a previous installation")
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(BACKUPS, 0o700)
    backup = BACKUPS / "server-pre-generic-v1.py"
    if backup.exists():
        raise FileExistsError("backup already exists, refusing silent overwrite")
    shutil.copy2(SERVER, backup)
    os.chmod(backup, 0o600)
    WORKSPACE.mkdir(parents=True, exist_ok=True, mode=0o700)
    import pwd
    import grp
    uid = pwd.getpwnam("mcp-relay").pw_uid
    gid = grp.getgrnam("mcp-relay").gr_gid
    os.chown(WORKSPACE.parent, uid, gid)
    os.chmod(WORKSPACE.parent, 0o700)
    os.chown(WORKSPACE, uid, gid)
    os.chmod(WORKSPACE, 0o700)
    DROPIN.parent.mkdir(parents=True, exist_ok=True)
    if DROPIN.exists():
        raise FileExistsError("drop-in already exists; refusing silent overwrite")
    # No new capabilities; a single audited writeable directory for the MCP account.
    DROPIN.write_text(
        "[Service]\nReadWritePaths=/var/lib/vps-mcp/workspaces\n",
        encoding="utf-8"
    )
    os.chmod(DROPIN, 0o644)
    tmpfd, tmppath = tempfile.mkstemp(prefix=".generic-", dir=MODULE.parent)
    try:
        with os.fdopen(tmpfd, "w", encoding="utf-8") as handle:
            handle.write(module_text)
        os.chown(tmppath, 0, gid)
        os.chmod(tmppath, 0o640)
        os.replace(tmppath, MODULE)
    finally:
        if os.path.exists(tmppath):
            os.unlink(tmppath)
    tmpfd, tmppath = tempfile.mkstemp(prefix=".server-", dir=SERVER.parent)
    try:
        with os.fdopen(tmpfd, "w", encoding="utf-8") as handle:
            handle.write(updated)
        mode = stat.S_IMODE(SERVER.stat().st_mode)
        os.chown(tmppath, 0, gid)
        os.chmod(tmppath, mode)
        os.replace(tmppath, SERVER)
    finally:
        if os.path.exists(tmppath):
            os.unlink(tmppath)
    try:
        invoke(["/usr/bin/python3", "-m", "py_compile", str(SERVER), str(MODULE)])
        invoke(["/usr/bin/systemctl", "daemon-reload"])
        invoke(["/usr/bin/systemctl", "restart", "r2-mcp.service"])
        invoke(["/usr/bin/systemctl", "is-active", "--quiet", "r2-mcp.service"])
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        shutil.copy2(backup, SERVER)
        MODULE.unlink(missing_ok=True)
        DROPIN.unlink(missing_ok=True)
        invoke(["/usr/bin/systemctl", "daemon-reload"])
        invoke(["/usr/bin/systemctl", "restart", "r2-mcp.service"])
        raise
    return {"status": "INSTALLED_MCP_RESTARTED",
            "backup": str(backup), "workspace": str(WORKSPACE),
            "new_tools": 3, "tunnel_restarted": False}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--module-source", type=Path, required=True)
    parser.add_argument("--apply", action="store_true",
                        help="explicitly authorize filesystem changes and MCP restart")
    args = parser.parse_args()
    source = SERVER.read_text(encoding="utf-8")
    next_source = add_registration(source)
    compile(next_source, str(SERVER), "exec")
    compile(args.module_source.read_text(encoding="utf-8"), str(MODULE), "exec")
    if not args.apply:
        print({"status": "DRY_RUN_PASS", "server_changed": next_source != source})
        return
    print(install(args.module_source))

if __name__ == "__main__":
    main()

"""通用 VPS MCP：只读状态、受限文档读取、固定仓库 Git Stage。"""
from __future__ import annotations
import fcntl
import json
import socket
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

PROJECTS = {"r2-relay": "https://github.com/sunxu/r2-relay.git"}
WORKSPACE = Path("/var/lib/vps-mcp/workspaces")
SHA = re.compile(r"[0-9a-f]{40}\Z")
DOC_EXT = {".md", ".txt", ".json", ".yaml", ".yml"}

def run_fixed(argv, *, cwd=None, timeout=15):
    try:
        p = subprocess.run(argv, shell=False, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace", check=False,
            cwd=cwd, timeout=timeout, env={
                "HOME": "/var/lib/vps-mcp", "PATH": "/usr/bin:/bin",
                "LANG": "C", "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
            })
        return {"exit_code": p.returncode, "stdout": p.stdout[-1500:],
                "stderr": p.stderr[-500:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit_code": 1, "error": type(exc).__name__}

def project_dir(project, base=WORKSPACE):
    if project not in PROJECTS:
        return None
    try:
        s = base.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_mode & 0o022:
            return None
        if base.resolve(strict=True) != base:
            return None
        target = base / project
        if target.is_symlink() or (target.exists() and
                (not target.is_dir() or target.resolve(strict=True) != target)):
            return None
        return target
    except OSError:
        return None

def system_snapshot():
    return {
        "disk": run_fixed(["/usr/bin/df", "-h", "/"]),
        "memory": run_fixed(["/usr/bin/free", "-h"]),
        "services": {name: run_fixed(["/usr/bin/systemctl", "is-active", name])
            for name in ("r2-mcp.service", "r2-mcp-tunnel.service",
                         "r2-relay-vps.service", "caddy.service")},
        "listeners": run_fixed(["/usr/bin/ss", "-lnt"]),
    }

def doc_read(project, relative_path, *, base=WORKSPACE):
    root = project_dir(project, base)
    if root is None:
        return {"error": "project_or_workspace_unavailable"}
    if not isinstance(relative_path, str) or not relative_path:
        return {"error": "invalid_path"}
    path = PurePosixPath(relative_path)
    parts = path.parts
    if (path.is_absolute() or ".." in parts or "\\" in relative_path
            or any(part in ("", ".") for part in parts)
            or not (parts[0] == "docs" or parts == ("README.md",))
            or path.suffix.lower() not in DOC_EXT):
        return {"error": "path_not_allowed"}
    try:
        node = root
        for part in parts:
            node = node / part
            if stat.S_ISLNK(node.lstat().st_mode):
                return {"error": "symlink_denied"}
        if not node.is_file() or node.stat().st_size > 65536:
            return {"error": "not_plain_small_file"}
        return {"path": str(path), "content": node.read_text(encoding="utf-8")}
    except (OSError, UnicodeError, ValueError):
        return {"error": "file_not_available"}

def _stage(project, commit, *, base=WORKSPACE):
    if project not in PROJECTS:
        return {"error": "project_not_allowed"}
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        return {"error": "full_sha_required"}
    target = project_dir(project, base)
    if target is None:
        return {"error": "workspace_unavailable"}
    try:
        fd = os.open(base / ("." + project + ".lock"),
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        return {"error": "lock_unavailable"}
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"error": "stage_in_progress"}
        if target.exists():
            return {"error": "stage_already_exists"}
        try:
            target.mkdir(mode=0o700)
        except OSError:
            return {"error": "stage_create_failed"}
        operations = [
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "init", "-q"],
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "remote",
             "add", "origin", PROJECTS[project]],
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null",
             "-c", "protocol.file.allow=never", "fetch", "--no-tags",
             "--depth=1", "origin", commit],
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "checkout",
             "--detach", "--force", "FETCH_HEAD"],
            ["/usr/bin/git", "rev-parse", "HEAD"],
        ]
        for idx, args in enumerate(operations):
            result = run_fixed(args, cwd=target,
                               timeout=120 if idx == 2 else 20)
            if result["exit_code"] != 0:
                return {"status": "FAILED", "step": idx + 1,
                        "error": "git_stage_failed"}
            if idx == 4 and result["stdout"].strip() != commit:
                return {"status": "FAILED", "error": "sha_mismatch"}
        return {"status": "STAGED_NOT_DEPLOYED", "project": project,
                "commit": commit, "path": str(target)}

def git_stage(project, commit):
    return _stage(project, commit)


def _r2_install_request(action: str):
    if action not in ("r2_install_dry_run", "r2_install_apply",
                       "r2_runtime_smoke", "r2_service_prepare"):
        return {"error": "action_denied"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(700)
            connection.connect("/run/r2-rootd/control.sock")
            connection.sendall(json.dumps({"action": action}).encode())
            buffer = bytearray()
            while True:
                item = connection.recv(4096)
                if not item:
                    break
                buffer.extend(item)
                if len(buffer) > 16384:
                    return {"error": "rootd_response_too_large"}
        payload = json.loads(buffer)
        if not isinstance(payload, dict):
            return {"error": "rootd_response_invalid"}
        return payload
    except (OSError, UnicodeError, ValueError):
        return {"error": "rootd_unavailable"}

def register(mcp):
    @mcp.tool()
    def vps_system_snapshot() -> dict:
        """只读磁盘、内存、已知服务和监听状态。"""
        return system_snapshot()

    @mcp.tool()
    def vps_project_doc_read(project: str, relative_path: str) -> dict:
        """读取固定 Git 工作区公开文档；拒绝私有配置与符号链接。"""
        return doc_read(project, relative_path)

    @mcp.tool()
    def vps_git_stage(project: str, commit: str) -> dict:
        """仅下载预先审批的固定仓库 SHA，不执行或安装任何代码。"""
        return git_stage(project, commit)

    @mcp.tool()
    def vps_r2_install_prepare(apply: bool = False) -> dict:
        """固定 Commit 的 R2 Relay 安装准备；默认只校验，apply 会写入 /opt/r2-relay，但不启动服务。"""
        return _r2_install_request(
            "r2_install_apply" if apply else "r2_install_dry_run")

    @mcp.tool()
    def vps_r2_runtime_smoke() -> dict:
        """非特权、无真实 R2 网络的本机 ASGI 健康和鉴权烟测。"""
        return _r2_install_request("r2_runtime_smoke")

    @mcp.tool()
    def vps_r2_service_prepare() -> dict:
        """仅创建固定 systemd unit 和本机随机令牌（不回显），不启动或启用服务。"""
        return _r2_install_request("r2_service_prepare")

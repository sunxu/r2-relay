"""Read-only, allowlisted inspection for the R2 Relay release deployment layout."""
from pathlib import Path
import os
import re
import stat
import subprocess

SERVICES = ("r2-relay-vps.service", "caddy.service", "docker.service",
            "r2-mcp.service", "r2-mcp-tunnel.service")
FILES = (
    "/opt/r2-relay",  # retained legacy tree; not the active release
    "/opt/r2-relay-current",
    "/opt/r2-relay-releases",
    "/opt/r2-relay-current/scripts/deployment_preflight.py",
    "/var/backups/r2-relay/deploy",
    "/etc/r2-relay/vps.env",
    "/etc/systemd/system/r2-relay-vps.service",
    "/etc/systemd/system/caddy.service",
    "/etc/systemd/system/docker.service",
)
PORTS = (22, 443, 8443, 18085, 18765)
CURRENT = Path("/opt/r2-relay-current")
RELEASES = Path("/opt/r2-relay-releases")
UNIT = "r2-relay-vps.service"
SYSTEMD_PROPERTIES = (
    "FragmentPath", "DropInPaths", "User", "Group", "WorkingDirectory",
    "EnvironmentFiles", "ExecStart", "ActiveState",
)


def _run(args):
    try:
        p = subprocess.run(args, shell=False, capture_output=True, text=True,
                           check=False, timeout=5,
                           env={"PATH": "/usr/bin:/bin", "LANG": "C"})
        return p.returncode, p.stdout[:12000]
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def inspect_path(path: Path) -> dict:
    """Use lstat so symlinks are reported, never silently followed."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False, "error": "not_found"}
    except PermissionError:
        return {"exists": None, "error": "permission_denied"}
    except OSError as exc:
        return {"exists": None, "error": f"os_error_{exc.errno or 'unknown'}"}

    mode = info.st_mode
    kind = ("symlink" if stat.S_ISLNK(mode)
            else "directory" if stat.S_ISDIR(mode)
            else "file" if stat.S_ISREG(mode) else "other")
    result = {"exists": True, "type": kind, "mode": oct(mode & 0o777),
              "uid": info.st_uid, "gid": info.st_gid}
    if kind == "symlink":
        try:
            result["target"] = os.readlink(path)
        except OSError:
            result["target"] = "unavailable"
    return result


def inspect_current_release(expected_uid=0) -> dict:
    """Resolve only a direct child of the fixed release root with a matching marker."""
    try:
        current_stat = CURRENT.lstat()
    except FileNotFoundError:
        return {"status": "MISSING", "reason": "current_pointer_missing"}
    except OSError as exc:
        return {"status": "UNKNOWN", "reason": f"current_pointer_os_error_{exc.errno or 'unknown'}"}
    if not stat.S_ISLNK(current_stat.st_mode) or current_stat.st_uid != expected_uid:
        return {"status": "INVALID", "reason": "current_pointer_not_root_symlink"}
    try:
        releases_root = RELEASES.resolve(strict=True)
        target_text = os.readlink(CURRENT)
        release = (CURRENT.parent / target_text).resolve(strict=True)
        if release.parent != releases_root or not release.is_dir():
            return {"status": "INVALID", "reason": "current_target_outside_release_root"}
        commit = release.name
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            return {"status": "INVALID", "reason": "release_name_not_full_sha"}
        marker = release / ".deployment-commit"
        marker_stat = marker.lstat()
        if (not stat.S_ISREG(marker_stat.st_mode) or marker_stat.st_uid != expected_uid
                or marker_stat.st_mode & 0o027):
            return {"status": "INVALID", "reason": "commit_marker_permissions_invalid"}
        if marker.read_text(encoding="ascii").strip() != commit:
            return {"status": "INVALID", "reason": "commit_marker_mismatch"}
        release_stat = release.lstat()
        return {
            "status": "VALID",
            "commit": commit,
            "path": str(release),
            "release_mode": oct(release_stat.st_mode & 0o777),
            "release_uid": release_stat.st_uid,
            "release_gid": release_stat.st_gid,
        }
    except FileNotFoundError:
        return {"status": "INVALID", "reason": "current_target_missing"}
    except (OSError, UnicodeError, ValueError) as exc:
        return {"status": "UNKNOWN", "reason": f"release_inspection_failed_{type(exc).__name__}"}


def inspect_systemd_unit() -> dict:
    args = ["/usr/bin/systemctl", "show", UNIT, "--no-pager"]
    for prop in SYSTEMD_PROPERTIES:
        args.extend(["--property", prop])
    code, out = _run(args)
    if code != 0:
        return {"query_ok": False}
    fields = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key in SYSTEMD_PROPERTIES:
                fields[key] = value
    # Never query or return Environment, EnvironmentFile contents, or process environ.
    return {
        "query_ok": True,
        "fragment_path": fields.get("FragmentPath", ""),
        "drop_in_paths": fields.get("DropInPaths", ""),
        "user": fields.get("User", ""),
        "group": fields.get("Group", ""),
        "working_directory": fields.get("WorkingDirectory", ""),
        "environment_files": fields.get("EnvironmentFiles", ""),
        "exec_start": fields.get("ExecStart", ""),
        "active_state": fields.get("ActiveState", ""),
        "release_layout": (
            fields.get("WorkingDirectory") == str(CURRENT)
            and str(CURRENT) + "/.venv/bin/uvicorn" in fields.get("ExecStart", "")
            and str(CURRENT) + "/vps" in fields.get("ExecStart", "")
        ),
    }


def deployment_inspect() -> dict:
    """Inspect fixed services, release pointers, metadata and listeners; never mutate."""
    services = {}
    for name in SERVICES:
        code, out = _run(["/usr/bin/systemctl", "show", name, "--no-pager",
                          "--property=LoadState", "--property=ActiveState"])
        fields = dict(line.split("=", 1) for line in out.splitlines()
                      if line.startswith(("LoadState=", "ActiveState=")))
        services[name] = {
            "load": fields.get("LoadState", "unknown"),
            "active": fields.get("ActiveState", "unknown"),
            "query_ok": code == 0,
        }
    files = {name: inspect_path(Path(name)) for name in FILES}
    listeners = {str(port): [] for port in PORTS}
    code, output = _run(["/usr/bin/ss", "-H", "-lnt"])
    if code == 0:
        for line in output.splitlines():
            columns = line.split()
            if len(columns) < 4 or columns[0] != "LISTEN":
                continue
            match = re.fullmatch(r"(\[[^\]]+\]|[^:\s]+):(\d+)", columns[3])
            if match and int(match.group(2)) in PORTS:
                entry = listeners[match.group(2)]
                if match.group(1) not in entry:
                    entry.append(match.group(1))
    return {
        "schema": "r2_relay_deployment_inspect_readonly_v2",
        "services": services,
        "files": files,
        "current_release": inspect_current_release(),
        "systemd": inspect_systemd_unit(),
        "listening_ports": listeners,
        "expected_port_layout": {
            "443": "present (existing sing-box listener)",
            "8443": "present (Caddy HTTPS)",
            "18085": "127.0.0.1 only (R2 Relay API)",
        },
        "modified": False,
        "credentials_read": False,
        "environment_file_contents_read": False,
        "real_r2_tested": False,
    }

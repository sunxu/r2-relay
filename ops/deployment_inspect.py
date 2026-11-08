"""Read-only, allowlisted inspection for a Debian R2 Relay MCP connector."""
from pathlib import Path
import re
import subprocess

SERVICES = ("r2-relay-vps.service", "caddy.service", "docker.service",
            "r2-mcp.service", "r2-mcp-tunnel.service")
FILES = ("/opt/r2-relay",
         "/opt/r2-relay/scripts/deployment_preflight.py",
         "/etc/r2-relay/vps.env",
         "/etc/systemd/system/r2-relay-vps.service",
         "/etc/systemd/system/caddy.service",
         "/etc/systemd/system/docker.service")
PORTS = (22, 443, 8443, 18085, 18765)


def _run(args):
    try:
        p = subprocess.run(args, shell=False, capture_output=True, text=True,
                           check=False, timeout=5,
                           env={"PATH": "/usr/bin:/bin", "LANG": "C"})
        return p.returncode, p.stdout[:12000]
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def deployment_inspect() -> dict:
    """Inspect a fixed set of services, paths and listening ports only."""
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
    files = {}
    for name in FILES:
        path = Path(name)
        try:
            info = path.lstat()
            files[name] = {
                "exists": True,
                "type": ("symlink" if path.is_symlink()
                         else "directory" if path.is_dir()
                         else "file" if path.is_file() else "other"),
                "mode": oct(info.st_mode & 0o777),
            }
        except OSError:
            files[name] = {"exists": False}
    listeners = {str(port): [] for port in PORTS}
    code, text = _run(["/usr/bin/ss", "-H", "-lnt"])
    if code == 0:
        for line in text.splitlines():
            columns = line.split()
            if len(columns) < 4 or columns[0] != "LISTEN":
                continue
            match = re.fullmatch(r"(\[[^\]]+\]|[^:\s]+):(\d+)", columns[3])
            if match and int(match.group(2)) in PORTS:
                entry = listeners[match.group(2)]
                if match.group(1) not in entry:
                    entry.append(match.group(1))
    return {
        "schema": "r2_relay_deployment_inspect_readonly_v1",
        "services": services,
        "files": files,
        "listening_ports": listeners,
        "modified": False,
        "credentials_read": False,
        "real_r2_tested": False,
    }

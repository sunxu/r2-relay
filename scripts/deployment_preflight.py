"""Strict offline config validator: no network or service execution."""
from __future__ import annotations
import argparse
import configparser
import json
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_VPS = ("R2_RELAY_TOKEN", "R2_RELAY_CACHE_DIR", "R2_RELAY_MAX_EXECUTIONS",
                "R2_RELAY_MAX_CONCURRENT", "R2_RELAY_EXECUTION_TTL",
                "R2_RELAY_MAX_PART_BYTES", "R2_RELAY_MIN_FREE_BYTES",
                "R2_RELAY_MAX_RETAINED_RESULT_BYTES")
REQUIRED_LOCAL = ("R2_RELAY_VPS_URL", "R2_RELAY_TOKEN", "R2_RELAY_BUFFER_BUDGET")


def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("invalid environment syntax")
        name, value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values:
            raise ValueError("invalid or duplicate environment name")
        if any(ch in value for ch in ("\n", "\r", "\x00", "$(", "`")):
            raise ValueError("unsafe environment syntax")
        value = value.strip().strip('"').strip("'")
        values[name] = value
    return values


def validate(root=ROOT, mode="template", vps_env=None, local_env=None):
    errors = []
    def check(condition, message):
        if not condition:
            errors.append(message)

    unit = configparser.ConfigParser(interpolation=None)
    unit.optionxform = str
    try:
        unit.read_string((root / "deploy/vps.service").read_text())
        service = unit["Service"]
        required = {
            "User": "r2relay", "Group": "r2relay",
            "EnvironmentFile": "/etc/r2-relay/vps.env",
            "ProtectSystem": "strict", "NoNewPrivileges": "true",
            "ProtectHome": "true", "PrivateTmp": "true",
            "PrivateDevices": "true", "UMask": "0077",
        }
        for key, value in required.items():
            check(service.get(key, "").lower() == value.lower(),
                  "systemd unsafe setting: " + key)
        command = service.get("ExecStart", "")
        for requirement in ("uvicorn", "--factory", "--host 127.0.0.1",
                            "--port 18085", "--workers 1"):
            check(requirement in command, "systemd ExecStart missing " + requirement)
        check("/var/lib/r2-relay/cache" in service.get("ReadWritePaths", "").split(),
              "systemd writable cache path mismatch")
        source = (root / "deploy/Caddyfile.example").read_text()
        caddy = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
        check("auto_https disable_redirects" in caddy, "Caddy redirects not disabled")
        check("https://{$R2_RELAY_HOST}:8443" in caddy, "Caddy HTTPS 8443 required")
        check("tls {$R2_RELAY_TLS_CERT} {$R2_RELAY_TLS_KEY}" in caddy,
              "Caddy external certificate required")
        check(bool(re.search(r"reverse_proxy\s+127\.0\.0\.1:18085", caddy)),
              "Caddy upstream must be loopback 18085")
        check(not re.search(r"(?m)^\s*(?:http://|:443\b|:80\b)", caddy),
              "Caddy must not claim 80 or 443")
    except (OSError, configparser.Error, KeyError):
        errors.append("systemd/Caddy template missing or malformed")

    vp = vps_env or root / "deploy/vps.env.example"
    lp = local_env or root / "deploy/local.env.example"
    try:
        vps, local = read_env(vp), read_env(lp)
    except (OSError, ValueError):
        # Never put private file contents in error text.
        errors.append("invalid environment file")
        vps, local = {}, {}
    for name in REQUIRED_VPS:
        check(name in vps, "missing VPS setting " + name)
    for name in REQUIRED_LOCAL:
        check(name in local, "missing local setting " + name)
    numbers = {
        "R2_RELAY_MAX_EXECUTIONS": (1, 100000),
        "R2_RELAY_MAX_CONCURRENT": (1, 1024),
        "R2_RELAY_EXECUTION_TTL": (1, 86400),
        "R2_RELAY_MAX_PART_BYTES": (1, 33554432),
        "R2_RELAY_MIN_FREE_BYTES": (1, 2**50),
        "R2_RELAY_MAX_RETAINED_RESULT_BYTES": (1, 2**40),
    }
    for name, (lo, hi) in numbers.items():
        value = vps.get(name, "")
        check(value.isdecimal() and lo <= int(value) <= hi,
              "invalid VPS bound " + name)
    buffer = local.get("R2_RELAY_BUFFER_BUDGET", "")
    check(buffer.isdecimal() and 1 <= int(buffer) <= 2**40 if buffer.isdecimal()
          else False, "invalid local buffer budget")
    check(vps.get("R2_RELAY_CACHE_DIR") == "/var/lib/r2-relay/cache",
          "cache directory differs from systemd sandbox")
    try:
        endpoint = urlsplit(local.get("R2_RELAY_VPS_URL", ""))
        check(endpoint.scheme == "https" and bool(endpoint.hostname)
              and endpoint.port == 8443 and not endpoint.username
              and not endpoint.password and not endpoint.query and not endpoint.fragment,
              "local VPS endpoint must be https://HOST:8443")
    except ValueError:
        errors.append("local VPS endpoint malformed")
    if mode == "live-files":
        for path in (vp, lp):
            try:
                check(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0,
                      "private environment file permissions are too broad")
            except OSError:
                errors.append("private environment file missing")
        token = vps.get("R2_RELAY_TOKEN", "")
        check(len(token) >= 32 and token == local.get("R2_RELAY_TOKEN")
              and not token.startswith(("REPLACE_", "YOUR_", "CHANGEME")),
              "runtime token missing/weak/mismatched")
    return {
        "schema": "r2_relay_deploy_preflight_safe_v1",
        "status": "PASS" if not errors else "FAIL", "mode": mode,
        "errors": errors, "secrets_emitted": False,
        "network_used": False, "real_vps_tested": False, "real_r2_tested": False,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Offline deployment preflight")
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--mode", choices=("template", "live-files"), default="template")
    p.add_argument("--vps-env", type=Path)
    p.add_argument("--local-env", type=Path)
    p.add_argument("--report", type=Path)
    args = p.parse_args(argv)
    if args.mode == "live-files" and (not args.vps_env or not args.local_env):
        p.error("live-files needs --vps-env and --local-env")
    result = validate(args.root, args.mode, args.vps_env, args.local_env)
    output = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    print(output, end="")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, encoding="utf-8")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

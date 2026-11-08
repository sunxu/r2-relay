#!/usr/bin/env python3
"""Fixed IP Caddy deployment. No shell, custom paths, token disclosure, or 443 mutation."""
from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import grp
import shutil
import stat
import subprocess
import tempfile
import ssl
import urllib.request

IP4 = "104.224.168.192"
IP6 = "2607:8700:5500:a47f::2"
BINARY = Path("/usr/local/libexec/r2-caddy")
CONFIG = Path("/etc/r2-relay/Caddyfile.ip")
UNIT = Path("/etc/systemd/system/caddy.service")
DATA = Path("/var/lib/r2-caddy")
CA = DATA / "data/caddy/pki/authorities/local/root.crt"
SERVICE = "caddy.service"
API = "r2-relay-vps.service"
IDENTITY = "r2caddy"

CADDYFILE = """{
    auto_https disable_redirects
    admin off
}
https://104.224.168.192:8443, https://[2607:8700:5500:a47f::2]:8443 {
    tls internal
    reverse_proxy 127.0.0.1:18085
}
"""
SYSTEMD = """[Unit]
Description=R2 Relay HTTPS 8443 (private IP CA)
After=network-online.target r2-relay-vps.service
Wants=network-online.target
Requires=r2-relay-vps.service

[Service]
Type=simple
User=r2caddy
Group=r2caddy
Environment=HOME=/var/lib/r2-caddy
Environment=XDG_DATA_HOME=/var/lib/r2-caddy/data
Environment=XDG_CONFIG_HOME=/var/lib/r2-caddy/config
WorkingDirectory=/var/lib/r2-caddy
ExecStart=/usr/local/libexec/r2-caddy run --environ --config /etc/r2-relay/Caddyfile.ip --adapter caddyfile
ExecReload=/usr/local/libexec/r2-caddy reload --config /etc/r2-relay/Caddyfile.ip --adapter caddyfile
Restart=on-failure
RestartSec=4
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ReadWritePaths=/var/lib/r2-caddy
UMask=0077

[Install]
WantedBy=multi-user.target
"""

def run(args, *, cwd=None, timeout=50, ok=True):
    try:
        p = subprocess.run(args, cwd=cwd, shell=False, check=False,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env={"PATH":"/usr/sbin:/usr/bin:/sbin:/bin",
                                "HOME":"/", "LANG":"C", "DEBIAN_FRONTEND":"noninteractive"})
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("fixed_command_unavailable") from None
    if ok and p.returncode:
        raise ValueError("fixed_command_failed")
    return p.stdout.strip()

def listeners(port):
    output = run(["/usr/bin/ss", "-H", "-lnt", "sport", "=", f":{port}"])
    result = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 5:
            raise ValueError("listener_parse_failed")
        result.append(fields[3])
    return sorted(result)

def service_state(name):
    return run(["/usr/bin/systemctl", "show", name, "-p", "ActiveState", "--value"])

def preflight():
    if os.geteuid() != 0:
        raise ValueError("root_required")
    assert ipaddress.ip_address(IP4).version == 4
    assert ipaddress.ip_address(IP6).version == 6
    if service_state(API) != "active" or listeners(18085) != ["127.0.0.1:18085"]:
        raise ValueError("api_not_loopback_ready")
    if not listeners(443):
        raise ValueError("existing_443_not_listening")
    if listeners(8443):
        raise ValueError("port_8443_occupied")
    if UNIT.exists() or UNIT.is_symlink() or CONFIG.exists() or CONFIG.is_symlink():
        raise ValueError("caddy_existing_configuration")
    if BINARY.exists() or BINARY.is_symlink() or DATA.exists() or DATA.is_symlink():
        raise ValueError("caddy_existing_files")
    if service_state(SERVICE) not in ("inactive", "failed"):
        raise ValueError("caddy_service_already_present")
    return {"status":"HTTPS_PREFLIGHT_PASS", "ipv4":IP4,"ipv6":IP6,
            "port_443_preserved":True, "port_8443_free":True,
            "api_loopback_ready":True, "modified":False}

def install_binary():
    # Download a signed-indexed Debian package and extract ONLY its binary;
    # dpkg post-install scripts must never execute or auto-start a service.
    with tempfile.TemporaryDirectory(prefix="r2-caddy-pkg-") as tmp:
        directory = Path(tmp)
        os.chmod(directory, 0o755)
        run(["/usr/bin/apt-get", "download", "caddy"], cwd=directory, timeout=90)
        packages = list(directory.glob("caddy_*.deb"))
        if len(packages) != 1 or packages[0].is_symlink():
            raise ValueError("caddy_package_missing")
        extracted = directory / "pkg"
        extracted.mkdir()
        run(["/usr/bin/dpkg-deb", "-x", str(packages[0]), str(extracted)])
        source = extracted / "usr/bin/caddy"
        if not source.is_file() or source.is_symlink():
            raise ValueError("caddy_binary_missing")
        if source.stat().st_size > 150_000_000:
            raise ValueError("caddy_binary_too_large")
        fd = os.open(BINARY, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o755)
        with os.fdopen(fd, "wb") as target, source.open("rb") as original:
            shutil.copyfileobj(original, target)
        os.chmod(BINARY, 0o755)
        run([str(BINARY), "version"],timeout=10)

def write_exclusive(path, content, mode):
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,"w",encoding="utf-8") as f:
        f.write(content)
    os.chmod(path,mode)

def prepare():
    initial=preflight()
    created=[]
    try:
        install_binary()
        created.append(BINARY)
        if not shutil.which("useradd"):
            raise ValueError("useradd_missing")
        if not any(p.pw_name==IDENTITY for p in pwd.getpwall()):
            run(["/usr/sbin/useradd","--system","--home-dir",str(DATA),
                 "--shell","/usr/sbin/nologin",IDENTITY])
        owner=pwd.getpwnam(IDENTITY).pw_uid
        group=grp.getgrnam(IDENTITY).gr_gid
        DATA.mkdir(mode=0o700)
        created.append(DATA)
        os.chown(DATA,owner,group)
        os.chmod(DATA,0o700)
        write_exclusive(CONFIG,CADDYFILE,0o644)
        created.append(CONFIG)
        write_exclusive(UNIT,SYSTEMD,0o644)
        created.append(UNIT)
        run(["/usr/bin/systemctl","daemon-reload"])
        # Caddyfile adapter validation only: do NOT start a listener.
        run(["/usr/sbin/runuser","-u",IDENTITY,"--",
             str(BINARY),"validate","--config",str(CONFIG),
             "--adapter","caddyfile"], timeout=25)
        if listeners(443)!=listeners(443) or listeners(8443):
            raise ValueError("unexpected_listener")
        return {"status":"HTTPS_PREPARED_NOT_STARTED",
                "service_started":False,"auto_enabled":False,
                "private_ca_ready":False,"port_8443_listening":False,
                "port_443_preserved":True,
                "ipv4":IP4,"ipv6":IP6}
    except Exception:
        for path in reversed(created):
            try:
                if path == DATA:
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        run(["/usr/bin/systemctl","daemon-reload"],ok=False)
        raise

def check_prepared():
    if os.geteuid()!=0: raise ValueError("root_required")
    for path in (UNIT,CONFIG,BINARY):
        st=path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=0:
            raise ValueError("prepared_file_invalid")
    if UNIT.read_text()!=SYSTEMD or CONFIG.read_text()!=CADDYFILE:
        raise ValueError("prepared_config_mismatch")
    if service_state(API)!="active" or listeners(18085)!=["127.0.0.1:18085"]:
        raise ValueError("api_not_loopback_ready")
    if service_state(SERVICE)!="inactive":
        raise ValueError("caddy_not_inactive")
    if listeners(8443):
        raise ValueError("8443_not_free")

def local_tls(ip):
    address=f"https://[{ip}]:8443/healthz" if ":" in ip else f"https://{ip}:8443/healthz"
    context=ssl.create_default_context(cafile=str(CA))
    context.check_hostname=True
    opener=urllib.request.build_opener(urllib.request.HTTPSHandler(context=context),
                                      urllib.request.ProxyHandler({}))
    with opener.open(address,timeout=8) as response:
        if response.status!=200 or b'"status":"ok"' not in response.read(256):
            raise ValueError("tls_health_check_failed")

def fingerprint():
    if not CA.exists() or CA.is_symlink():
        raise ValueError("ca_root_certificate_missing")
    data=CA.read_bytes()
    if b"BEGIN CERTIFICATE" not in data or len(data)>50000:
        raise ValueError("ca_root_certificate_invalid")
    return {"status":"CA_FINGERPRINT_READY","public_ca_path":str(CA),
            "ca_sha256":hashlib.sha256(data).hexdigest(),
            "private_key_emitted":False,"ca_pem_emitted":False}

def start():
    check_prepared()
    before_443=listeners(443)
    invoked=False
    try:
        invoked=True
        run(["/usr/bin/systemctl","start",SERVICE],timeout=40)
        if service_state(SERVICE)!="active" or listeners(443)!=before_443:
            raise ValueError("service_or_443_changed")
        if not listeners(8443):
            raise ValueError("8443_not_listening")
        fp=fingerprint()
        local_tls(IP4)
        local_tls(IP6)
        if listeners(443)!=before_443:
            raise ValueError("443_changed_after_tls")
        return {"status":"HTTPS_IP_DUALSTACK_LOCAL_PASS",
                "ipv4":IP4,"ipv6":IP6, "listener_8443":listeners(8443),
                "port_443_preserved":True,
                "private_key_emitted":False,"real_r2_tested":False,
                "external_connectivity_verified":False, **fp}
    except Exception:
        if invoked:
            run(["/usr/bin/systemctl","stop",SERVICE],timeout=25,ok=False)
        raise

def main():
    p=argparse.ArgumentParser()
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument("--preflight",action="store_true")
    g.add_argument("--prepare",action="store_true")
    g.add_argument("--start",action="store_true")
    g.add_argument("--ca-fingerprint",action="store_true")
    a=p.parse_args()
    try:
        out=(preflight() if a.preflight else
             prepare() if a.prepare else
             start() if a.start else fingerprint())
    except Exception as exc:
        out={"status":"HTTPS_IP_CONTROL_FAILED","error_type":type(exc).__name__,
             "private_key_emitted":False}
    print(json.dumps(out,sort_keys=True))
    return 0 if out["status"] in ("HTTPS_PREFLIGHT_PASS","HTTPS_PREPARED_NOT_STARTED",
        "HTTPS_IP_DUALSTACK_LOCAL_PASS","CA_FINGERPRINT_READY") else 1

if __name__=="__main__":
    raise SystemExit(main())

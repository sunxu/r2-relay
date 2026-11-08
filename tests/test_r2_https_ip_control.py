"""Fixed Caddy IP TLS deploy safety contract. Tests run without VPS privileges."""
from pathlib import Path
import importlib.util
import pytest

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    m=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def test_no_domains_and_443_binding():
    m=load("https1","ops/r2_https_ip_control.py")
    assert m.IP4=="104.224.168.192"
    assert m.IP6=="2607:8700:5500:a47f::2"
    assert ":8443 {" in m.CADDYFILE
    assert "tls /etc/r2-caddy/tls/server.crt /etc/r2-caddy/tls/server.key" in m.CADDYFILE
    assert "IP:104.224.168.192,IP:2607:8700:5500:a47f::2" in (ROOT/"ops/r2_https_ip_control.py").read_text()
    assert "reverse_proxy 127.0.0.1:18085" in m.CADDYFILE
    assert "auto_https off" in m.CADDYFILE
    assert ":443 {" not in m.CADDYFILE and ":80 {" not in m.CADDYFILE
    assert "/etc/r2-caddy/Caddyfile" in m.SYSTEMD
    assert "User=r2caddy" in m.SYSTEMD and "NoNewPrivileges=true" in m.SYSTEMD
    assert "ProtectSystem=strict" in m.SYSTEMD
    assert "ReadWritePaths=/var/lib/r2-caddy" in m.SYSTEMD
    assert "tls internal" not in m.CADDYFILE

def test_preflight_no_mutation(monkeypatch):
    m=load("https2","ops/r2_https_ip_control.py")
    monkeypatch.setattr(m.os,"geteuid",lambda:0)
    monkeypatch.setattr(m,"service_state",lambda name:"active" if name==m.API else "inactive")
    monkeypatch.setattr(m,"listeners",lambda port: {443:["*:443"],18085:["127.0.0.1:18085"],8443:[]}[port])
    for path in (m.UNIT,m.CONFIG,m.BINARY,m.DATA):
        monkeypatch.setattr(type(path),"exists",lambda self:False)
        monkeypatch.setattr(type(path),"is_symlink",lambda self:False)
    assert m.preflight()["status"]=="HTTPS_PREFLIGHT_PASS"

def test_preflight_fails_if_8443_is_busy(monkeypatch):
    m=load("https3","ops/r2_https_ip_control.py")
    monkeypatch.setattr(m.os,"geteuid",lambda:0)
    monkeypatch.setattr(m,"service_state",lambda name:"active" if name==m.API else "inactive")
    monkeypatch.setattr(m,"listeners",lambda port: ["*:8443"] if port==8443 else
        ["*:443"] if port==443 else ["127.0.0.1:18085"])
    with pytest.raises(ValueError,match="occupied"):
        m.preflight()

def test_rootd_fixed_argv(monkeypatch):
    d=load("rootd_https","ops/r2_rootd.py")
    called=[]
    monkeypatch.setattr(d,"run",lambda args,timeout=180: called.append(args) or {"exit_code":0})
    for action,flag in (("r2_https_preflight","--preflight"),
                        ("r2_https_prepare","--prepare"),
                        ("r2_https_start","--start"),
                        ("r2_https_ca_fingerprint","--ca-fingerprint")):
        result=d.process_request('{"action":"'+action+'"}',123,123)
        assert result["exit_code"]==0 and called[-1]==[
            "/usr/bin/python3","/usr/local/libexec/r2_https_ip_control.py",flag]
    assert d.process_request('{"action":"r2_https_start","shell":"bash"}',123,123)["error"]=="invalid_request_schema"

def test_mcp_registration():
    mod=load("generic_https","ops/generic_vps_mcp.py")
    class App:
        def __init__(self):self.tools={}
        def tool(self):
            def inner(fn):self.tools[fn.__name__]=fn;return fn
            return inner
    app=App()
    mod.register(app)
    for name in ("vps_r2_https_preflight","vps_r2_https_prepare",
                 "vps_r2_https_start","vps_r2_https_ca_fingerprint"):
        assert name in app.tools
    assert mod._r2_install_request("arbitrary-shell")=={"error":"action_denied"}

def test_no_package_postinstall_or_disabling_validation():
    s=(ROOT/"ops/r2_https_ip_control.py").read_text()
    assert '"/usr/bin/dpkg-deb", "-x"' in s
    assert '"/usr/bin/apt-get", "download", "caddy"' in s
    assert '"install", "caddy"' not in s
    assert "verify=False" not in s
    assert '"-CA",str(CA),"-CAkey",str(CA_KEY)' in s
    assert '"subjectAltName=IP:104.224.168.192,IP:2607:8700:5500:a47f::2' in s
    assert "check_hostname = True" in s
    assert "server_hostname=ip" in s
    assert '"/usr/bin/systemctl","start",SERVICE' in s
    assert '"/usr/bin/systemctl","stop",SERVICE' in s
    assert '"/usr/bin/systemctl","enable"' not in s
    assert "sing-box.service" not in s

def test_ca_fingerprint_is_public_only(tmp_path,monkeypatch):
    m=load("https4","ops/r2_https_ip_control.py")
    pem=tmp_path/"root.crt"
    pem.write_text("-----BEGIN CERTIFICATE-----\nfoo\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(m,"CA",pem)
    r=m.fingerprint()
    assert r["status"]=="CA_FINGERPRINT_READY"
    assert len(r["ca_sha256"])==64
    assert r["private_key_emitted"] is False
    assert "pem" not in r and "key" not in r

def test_start_failed_tls_checks_stop_service(monkeypatch):
    m=load("https_rollback","ops/r2_https_ip_control.py")
    monkeypatch.setattr(m,"check_prepared",lambda:None)
    monkeypatch.setattr(m,"listeners",lambda port: ["*:443"] if port==443 else ["*:8443"])
    monkeypatch.setattr(m,"service_state",lambda name:"active")
    commands=[]
    monkeypatch.setattr(m,"run",lambda args,**kw: commands.append(args) or "")
    monkeypatch.setattr(m,"fingerprint",lambda: (_ for _ in ()).throw(ValueError("ca_missing")))
    with pytest.raises(ValueError,match="ca_missing"):
        m.start()
    assert ["/usr/bin/systemctl","start",m.SERVICE] in commands
    assert ["/usr/bin/systemctl","stop",m.SERVICE] in commands

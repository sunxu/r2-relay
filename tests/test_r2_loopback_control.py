from pathlib import Path
import importlib.util
import pytest

ROOT = Path(__file__).resolve().parents[1]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def test_fixed_rootd_actions(monkeypatch):
    d = load("rootd_check", "ops/r2_rootd.py")
    calls = []
    monkeypatch.setattr(d, "run", lambda argv,timeout=180:
                        calls.append(argv) or {"exit_code":0})
    for action in ("r2_service_verify","r2_loopback_start"):
        assert d.process_request('{"action":"'+action+'"}',12,12)["exit_code"] == 0
    assert calls[0] == ["/usr/bin/python3","/usr/local/libexec/r2_loopback_control.py"]
    assert calls[1] == calls[0] + ["--start"]
    assert "error" in d.process_request(
        '{"action":"r2_loopback_start","cmd":"sh"}',12,12)

def test_registration():
    m = load("mcp_check", "ops/generic_vps_mcp.py")
    class Fake:
        def __init__(self): self.names=[]
        def tool(self):
            def decorator(fn): self.names.append(fn.__name__); return fn
            return decorator
    f=Fake()
    m.register(f)
    assert "vps_r2_service_verify" in f.names
    assert "vps_r2_loopback_start" in f.names
    assert m._r2_install_request("root_shell") == {"error":"action_denied"}

def test_verify_fails_for_non_root(monkeypatch):
    m = load("loop_a", "ops/r2_loopback_control.py")
    monkeypatch.setattr(m.os,"geteuid",lambda:1000)
    with pytest.raises(ValueError,match="root_required"):
        m.verify()

def test_start_fails_closed(monkeypatch):
    m=load("loop_b","ops/r2_loopback_control.py")
    monkeypatch.setattr(m,"verify",lambda:{"service_state":"inactive",
                                          "port_18085_listening":True})
    with pytest.raises(ValueError,match="busy"):
        m.start()
    monkeypatch.setattr(m,"verify",lambda:{"service_state":"active",
                                          "port_18085_listening":False})
    with pytest.raises(ValueError,match="not_inactive"):
        m.start()

def test_no_secret_echo_or_443_action():
    text=(ROOT/"ops/r2_loopback_control.py").read_text()
    assert '"token_emitted": False' in text
    assert 'ProxyHandler({})' in text
    assert '["/usr/bin/systemctl", "start", SERVICE]' in text
    assert '["/usr/bin/systemctl", "stop", SERVICE]' in text
    assert "sing-box.service" not in text

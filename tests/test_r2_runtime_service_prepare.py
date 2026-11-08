from pathlib import Path
import importlib.util
import pytest

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    obj=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj

def test_rootd_fixed_actions(monkeypatch):
    rootd=load("rootd_runtime","ops/r2_rootd.py")
    calls=[]
    monkeypatch.setattr(rootd,"run",lambda argv,timeout=180:
                        calls.append((argv,timeout)) or {"exit_code":0})
    for action in ("r2_runtime_smoke","r2_service_prepare"):
        assert rootd.process_request('{"action":"'+action+'"}',42,42)["exit_code"]==0
    assert calls[0][0][:4]==["/usr/sbin/runuser","-u","r2relay","--"]
    assert calls[1][0]==["/usr/bin/python3","/usr/local/libexec/r2_service_prepare.py"]
    assert rootd.process_request('{"action":"r2_service_prepare","cmd":"bash"}',42,42)["error"]=="invalid_request_schema"

def test_mcp_registration():
    appmod=load("generic_runtime","ops/generic_vps_mcp.py")
    class Mcp:
        def __init__(self): self.tools=[]
        def tool(self):
            def deco(fn): self.tools.append(fn.__name__); return fn
            return deco
    m=Mcp()
    appmod.register(m)
    assert "vps_r2_runtime_smoke" in m.tools
    assert "vps_r2_service_prepare" in m.tools
    assert appmod._r2_install_request("arbitrary_shell")=={"error":"action_denied"}

def test_smoke_source_never_uses_production_token_or_network():
    source=(ROOT/"ops/r2_runtime_smoke.py").read_text()
    assert 'token="SMOKE_FAKE_LOCAL_TOKEN_ONLY"' in source
    assert 'TestClient(app)' in source
    assert 'client.get("/healthz")' in source
    assert 'client.get("/v1/metrics")' in source
    assert "requests.get" not in source
    assert "os.environ.get" not in source

def test_service_check_fails_closed(tmp_path,monkeypatch):
    mod=load("service_prep","ops/r2_service_prepare.py")
    monkeypatch.setattr(mod.os,"geteuid",lambda:0)
    monkeypatch.setattr(mod,"APP",tmp_path/"missing")
    with pytest.raises(RuntimeError,match="application_missing"):
        mod.check()
    s=(ROOT/"ops/r2_service_prepare.py").read_text()
    assert '"daemon-reload"' in s
    assert '"start"' not in s and '"enable"' not in s
    assert "secrets.token_urlsafe(48)" in s
    assert "0o600" in s

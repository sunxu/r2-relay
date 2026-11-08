"""Rootd and MCP integration guard tests for the fixed install action."""
import importlib.util
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    m=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def test_rootd_strict_action_schema_and_no_shell(monkeypatch):
    daemon=load("rootd","ops/r2_rootd.py")
    commands=[]
    monkeypatch.setattr(daemon,"run",lambda argv,timeout=180: commands.append((argv,timeout)) or {"exit_code":0})
    assert daemon.process_request('{"action":"r2_install_dry_run"}',999,999)["exit_code"]==0
    assert commands[-1][0]==["/usr/bin/python3","/usr/local/libexec/r2_install_prepare.py"]
    assert daemon.process_request('{"action":"r2_install_apply"}',999,999)["exit_code"]==0
    assert commands[-1][0][-1]=="--apply"
    for bad in ('{"action":"r2_install_apply","cmd":"sh"}',
                '{"action":"r2_install_apply","path":"/tmp"}'):
        assert "error" in daemon.process_request(bad,999,999)
    assert "r2_install_apply" in daemon.ACTIONS

def test_mcp_install_tool_registration(monkeypatch):
    module=load("generic","ops/generic_vps_mcp.py")
    class App:
        def __init__(self): self.items={}
        def tool(self):
            def decorate(fn): self.items[fn.__name__]=fn; return fn
            return decorate
    app=App()
    module.register(app)
    assert set(app.items)=={"vps_system_snapshot","vps_project_doc_read",
                            "vps_git_stage","vps_r2_install_prepare",
                            "vps_r2_runtime_smoke","vps_r2_service_prepare",
                            "vps_r2_service_verify","vps_r2_loopback_start"}
    called=[]
    monkeypatch.setattr(module,"_r2_install_request",lambda a: called.append(a) or {"ok":True})
    assert app.items["vps_r2_install_prepare"]()=={"ok":True}
    assert app.items["vps_r2_install_prepare"](True)=={"ok":True}
    assert called==["r2_install_dry_run","r2_install_apply"]

def test_denies_custom_action():
    module=load("generic2","ops/generic_vps_mcp.py")
    assert module._r2_install_request("arbitrary-shell")=={"error":"action_denied"}

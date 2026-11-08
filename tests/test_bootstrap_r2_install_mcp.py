from pathlib import Path
import importlib.util
import pytest

p=Path(__file__).resolve().parents[1]/"ops/bootstrap_r2_install_mcp.py"
s=importlib.util.spec_from_file_location("upgrade",p)
module=importlib.util.module_from_spec(s)
s.loader.exec_module(module)

def test_only_fixed_mcp_files_and_services():
    assert set(module.FILES) == {
        "ops/r2_rootd.py", "ops/r2_install_prepare.py",
        "ops/generic_vps_mcp.py"}
    assert module.SERVICES == ("r2-rootd.service","r2-mcp.service")
    assert all(str(p).startswith(("/opt/r2-mcp/","/usr/local/libexec/"))
               for p in module.FILES.values())

def test_reject_bad_sha_before_network():
    for sha in ("main","a"*39,"a"*41,"A"*40,"../../etc"):
        with pytest.raises(ValueError):
            module.get_files(sha)

def test_dry_run_preserves_files(monkeypatch,tmp_path):
    targets={}
    for source in module.FILES:
        dest=tmp_path/source.replace("/","_")
        dest.write_text("old")
        targets[source]=dest
    monkeypatch.setattr(module,"FILES",targets)
    monkeypatch.setattr(module.os,"geteuid",lambda:0)
    monkeypatch.setattr(module,"get_files",
                        lambda c: {x:b"x=1\n" for x in targets})
    assert module.install("a"*40,apply=False)["status"]=="UPGRADE_DRY_RUN_PASS"
    assert all(p.read_text()=="old" for p in targets.values())

def test_no_shell_and_preserve_tunnel():
    text=p.read_text()
    assert "shell=False" in text
    assert "sing-box.service" not in text
    assert "r2-mcp-tunnel.service" not in module.SERVICES

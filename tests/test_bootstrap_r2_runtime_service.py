from pathlib import Path
import importlib.util
import pytest

p=Path(__file__).resolve().parents[1]/"ops/bootstrap_r2_runtime_service.py"
spec=importlib.util.spec_from_file_location("runtime_bootstrap",p)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def test_fixed_upgrade_scope():
    assert set(mod.FILES)=={
        "ops/r2_rootd.py", "ops/generic_vps_mcp.py",
        "ops/r2_runtime_smoke.py", "ops/r2_service_prepare.py"}
    assert mod.SERVICES==("r2-rootd.service","r2-mcp.service")

def test_sha_validation():
    for sha in ("main","a"*39,"A"*40):
        with pytest.raises(ValueError): mod.get_files(sha)

def test_dry_run_no_changes(tmp_path,monkeypatch):
    files={name:tmp_path/Path(name).name for name in mod.FILES}
    monkeypatch.setattr(mod,"FILES",files)
    monkeypatch.setattr(mod.os,"geteuid",lambda:0)
    monkeypatch.setattr(mod,"get_files",lambda sha:{k:b"x=2\\n" for k in files})
    assert mod.install("a"*40,apply=False)["status"]=="UPGRADE_DRY_RUN_PASS"
    assert all(not v.exists() for v in files.values())

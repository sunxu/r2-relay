from pathlib import Path
import importlib.util
import pytest
p=Path(__file__).resolve().parents[1]/"ops/bootstrap_r2_https_ip.py"
spec=importlib.util.spec_from_file_location("https_boot",p)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
def test_fixed_upgrade_scope():
    assert set(mod.FILES)=={
        "ops/r2_rootd.py","ops/r2_https_ip_control.py","ops/generic_vps_mcp.py"}
    assert mod.SERVICES==("r2-rootd.service","r2-mcp.service")
    assert "sing-box.service" not in mod.SERVICES
def test_reject_malformed_sha():
    for bad in ("main","A"*40,"a"*39):
        with pytest.raises(ValueError):mod.get_files(bad)

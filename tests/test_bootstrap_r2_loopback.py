from pathlib import Path
import importlib.util
import pytest

file=Path(__file__).resolve().parents[1]/"ops/bootstrap_r2_loopback.py"
spec=importlib.util.spec_from_file_location("boot_loop",file)
b=importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

def test_upgrade_scope():
    assert set(b.FILES)=={"ops/r2_rootd.py","ops/generic_vps_mcp.py",
                          "ops/r2_loopback_control.py"}
    assert b.SERVICES==("r2-rootd.service","r2-mcp.service")

def test_no_ambiguous_sha():
    for sha in ("main","A"*40,"a"*39):
        with pytest.raises(ValueError):
            b.get_files(sha)

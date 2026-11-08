from pathlib import Path
import importlib.util
import os
import stat
import pytest

file = Path(__file__).resolve().parents[1] / "ops/bootstrap_r2_loopback.py"
spec = importlib.util.spec_from_file_location("boot_loop", file)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_upgrade_scope_includes_both_legacy_inspectors():
    assert set(module.FILES) == {
        "ops/r2_rootd.py",
        "ops/r2_loopback_control.py",
        "ops/generic_vps_mcp.py",
        "ops/deployment_inspect.py",
    }
    assert module.FILES["ops/deployment_inspect.py"] == Path(
        "/opt/r2-relay/scripts/deployment_inspect.py")
    assert module.SERVICES == ("r2-rootd.service", "r2-mcp.service")


def test_no_ambiguous_sha():
    for sha in ("main", "A" * 40, "a" * 39):
        with pytest.raises(ValueError):
            module.get_files(sha)


def test_backup_directory_is_unique_private_and_root_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "BACKUP_ROOT", tmp_path / "backups")
    first = module.create_backup_dir(expected_uid=os.geteuid(), expected_gid=os.getgid())
    second = module.create_backup_dir(expected_uid=os.geteuid(), expected_gid=os.getgid())
    assert first != second
    assert stat.S_IMODE(first.stat().st_mode) == 0o700
    assert stat.S_IMODE(second.stat().st_mode) == 0o700
    assert first.stat().st_uid == os.geteuid()

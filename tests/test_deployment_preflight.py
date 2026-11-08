"""P1 preflight tests run without external systems."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deploycheck", ROOT / "scripts/deployment_preflight.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def copy_deploy(tmp_path):
    target = tmp_path / "deploy"
    target.mkdir()
    for name in ("vps.service", "Caddyfile.example", "vps.env.example", "local.env.example"):
        (target / name).write_bytes((ROOT / "deploy" / name).read_bytes())
    return tmp_path


def test_default_templates_pass(tmp_path):
    result = module.validate(copy_deploy(tmp_path))
    assert result["status"] == "PASS", result
    assert not result["network_used"] and not result["secrets_emitted"]


@pytest.mark.parametrize("file,old,new", [
    ("vps.service", "--host 127.0.0.1", "--host 0.0.0.0"),
    ("vps.service", "ProtectSystem=strict", "ProtectSystem=false"),
    ("Caddyfile.example", ":8443", ":443"),
    ("Caddyfile.example", "reverse_proxy 127.0.0.1:18085", "reverse_proxy 0.0.0.0:18085"),
    ("vps.env.example", "R2_RELAY_MAX_PART_BYTES=33554432",
     "R2_RELAY_MAX_PART_BYTES=999999999"),
    ("local.env.example", "https://YOUR_VPS_HOST:8443", "http://YOUR_VPS_HOST:8443"),
])
def test_reject_invalid_config(tmp_path, file, old, new):
    root = copy_deploy(tmp_path)
    p = root / "deploy" / file
    assert old in p.read_text()
    p.write_text(p.read_text().replace(old, new))
    assert module.validate(root)["status"] == "FAIL"


def test_live_file_permissions_and_no_secret_echo(tmp_path):
    root = copy_deploy(tmp_path)
    secret = "SECRET_TEST_DONT_PRINT_" + "A" * 32
    vps = tmp_path / "vps.env"
    local = tmp_path / "local.env"
    vps.write_text((root / "deploy/vps.env.example").read_text().replace(
        "REPLACE_WITH_LONG_RANDOM_SECRET", secret))
    local.write_text((root / "deploy/local.env.example").read_text().replace(
        "REPLACE_WITH_LONG_RANDOM_SECRET", secret))
    vps.chmod(0o644)
    local.chmod(0o600)
    failed = module.validate(root, "live-files", vps, local)
    assert failed["status"] == "FAIL"
    assert secret not in str(failed)
    vps.chmod(0o600)
    assert module.validate(root, "live-files", vps, local)["status"] == "PASS"

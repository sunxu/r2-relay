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

def test_wrong_runtime_token_fails_without_leaking(tmp_path):
    root = copy_deploy(tmp_path)
    vps = tmp_path / "vps.env"
    local = tmp_path / "local.env"
    vps.write_text((root / "deploy/vps.env.example").read_text().replace(
        "REPLACE_WITH_LONG_RANDOM_SECRET", "secret-vps-" + "a" * 38))
    local.write_text((root / "deploy/local.env.example").read_text().replace(
        "REPLACE_WITH_LONG_RANDOM_SECRET", "secret-local-" + "b" * 38))
    vps.chmod(0o600)
    local.chmod(0o600)
    result = module.validate(root, "live-files", vps, local)
    assert result["status"] == "FAIL"
    assert "secret-vps" not in str(result)
    assert "secret-local" not in str(result)


def test_duplicate_env_name_fails(tmp_path):
    root = copy_deploy(tmp_path)
    target = root / "deploy/vps.env.example"
    target.write_text(target.read_text() + "\nR2_RELAY_TOKEN=duplicated\n")
    result = module.validate(root)
    assert result["status"] == "FAIL"
    assert result["secrets_emitted"] is False

@pytest.mark.parametrize("file,key,large", [
    ("vps.env.example", "R2_RELAY_MAX_EXECUTIONS", 100001),
    ("vps.env.example", "R2_RELAY_MAX_CONCURRENT", 1025),
    ("vps.env.example", "R2_RELAY_EXECUTION_TTL", 86401),
    ("vps.env.example", "R2_RELAY_MIN_FREE_BYTES", 2**50 + 1),
    ("vps.env.example", "R2_RELAY_MAX_RETAINED_RESULT_BYTES", 2**40 + 1),
    ("local.env.example", "R2_RELAY_BUFFER_BUDGET", 2**40 + 1),
])
def test_accepts_values_above_previous_arbitrary_ceilings(tmp_path, file, key, large):
    root = copy_deploy(tmp_path)
    target = root / "deploy" / file
    original = target.read_text()
    lines = [f"{key}={large}" if line.startswith(f"{key}=") else line
             for line in original.splitlines()]
    target.write_text("\n".join(lines) + "\n")
    result = module.validate(root)
    assert result["status"] == "PASS", result


@pytest.mark.parametrize("key", [
    "R2_RELAY_MAX_EXECUTIONS", "R2_RELAY_MAX_CONCURRENT", "R2_RELAY_EXECUTION_TTL",
    "R2_RELAY_MAX_PART_BYTES", "R2_RELAY_MIN_FREE_BYTES",
    "R2_RELAY_MAX_RETAINED_RESULT_BYTES",
])
def test_rejects_nonpositive_vps_config(tmp_path, key):
    root = copy_deploy(tmp_path)
    target = root / "deploy/vps.env.example"
    target.write_text("\n".join(
        f"{key}=0" if line.startswith(f"{key}=") else line
        for line in target.read_text().splitlines()
    ) + "\n")
    assert module.validate(root)["status"] == "FAIL"


def test_rejects_nonpositive_local_buffer_budget(tmp_path):
    root = copy_deploy(tmp_path)
    target = root / "deploy/local.env.example"
    target.write_text(target.read_text().replace(
        "R2_RELAY_BUFFER_BUDGET=536870912", "R2_RELAY_BUFFER_BUDGET=0"
    ))
    assert module.validate(root)["status"] == "FAIL"

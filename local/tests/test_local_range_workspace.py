"""The Mac test workspace bootstrap must parse on CI and remain isolated."""
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "poc" / "local_range_workspace.sh"


def test_workspace_setup_bash_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)


def test_workspace_setup_only_targets_isolated_config_and_checkout():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "workspace/r2-relay-local-range-test" in script
    assert "private/local-range-acceptance" in script
    assert "local-range-acceptance/downloads" in script
    assert 'R2_RELAY_CONFIG="$config"' in script
    assert "feat/local-httpx-range-backend" in script
    assert "local/poc/real_r2_download_acceptance.py" in script
    assert "R2_RELAY_REAL_GET_SPEC" in script
    # Do not run the existing production proxy or rewrite user-owned config.
    assert "launchctl" not in script
    assert "systemctl" not in script
    assert 'R2_RELAY_ENABLE_ADDON="1"' not in script

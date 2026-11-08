"""Regression tests for read-only inspection of the GitHub release layout."""
import importlib.util
import os
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "ops/deployment_inspect.py"
spec = importlib.util.spec_from_file_location("deployment_inspect", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_static_allowlists_cover_new_release_layout_without_secret_values():
    assert "/opt/r2-relay-current" in mod.FILES
    assert "/opt/r2-relay-releases" in mod.FILES
    assert "/var/backups/r2-relay/deploy" in mod.FILES
    assert "/etc/r2-relay/vps.env" in mod.FILES
    assert mod.PORTS == (22, 443, 8443, 18085, 18765)
    assert "Environment" not in mod.SYSTEMD_PROPERTIES
    assert "EnvironmentFiles" in mod.SYSTEMD_PROPERTIES
    assert not any("token" in item.lower() for item in mod.FILES)


def test_inspect_path_reports_symlink_without_following_it(tmp_path):
    target = tmp_path / "target"
    target.write_text("private")
    link = tmp_path / "link"
    link.symlink_to(target)
    result = mod.inspect_path(link)
    assert result["exists"] is True
    assert result["type"] == "symlink"
    assert result["target"] == str(target)


def make_release(tmp_path, sha, uid, gid):
    releases = tmp_path / "releases"
    releases.mkdir(mode=0o750)
    release = releases / sha
    release.mkdir(mode=0o750)
    marker = release / ".deployment-commit"
    marker.write_text(sha + "\n")
    marker.chmod(0o640)
    return releases, release


def test_current_release_reports_exact_valid_commit(tmp_path, monkeypatch):
    sha = "a" * 40
    releases, release = make_release(tmp_path, sha, os.geteuid(), os.getgid())
    current = tmp_path / "current"
    current.symlink_to(release)
    monkeypatch.setattr(mod, "RELEASES", releases)
    monkeypatch.setattr(mod, "CURRENT", current)
    result = mod.inspect_current_release(expected_uid=os.geteuid())
    assert result["status"] == "VALID"
    assert result["commit"] == sha
    assert result["path"] == str(release)


def test_current_release_rejects_escape_and_marker_mismatch(tmp_path, monkeypatch):
    sha = "b" * 40
    releases, release = make_release(tmp_path, sha, os.geteuid(), os.getgid())
    current = tmp_path / "current"
    outside = tmp_path / "outside"
    outside.mkdir()
    current.symlink_to(outside)
    monkeypatch.setattr(mod, "RELEASES", releases)
    monkeypatch.setattr(mod, "CURRENT", current)
    assert mod.inspect_current_release(expected_uid=os.geteuid()) == {
        "status": "INVALID", "reason": "current_target_outside_release_root"
    }
    current.unlink()
    current.symlink_to(release)
    (release / ".deployment-commit").write_text("c" * 40)
    assert mod.inspect_current_release(expected_uid=os.geteuid())["reason"] == "commit_marker_mismatch"


def test_inspect_systemd_reports_safe_metadata_only(monkeypatch):
    output = "\n".join((
        "FragmentPath=/etc/systemd/system/r2-relay-vps.service",
        "DropInPaths=",
        "User=r2relay",
        "Group=r2relay",
        "WorkingDirectory=/opt/r2-relay-current",
        "EnvironmentFiles=/etc/r2-relay/vps.env (ignore_errors=no)",
        "ExecStart={ path=/opt/r2-relay-current/.venv/bin/uvicorn ; argv[]=/opt/r2-relay-current/.venv/bin/uvicorn --app-dir /opt/r2-relay-current/vps }",
        "ActiveState=active",
    ))
    monkeypatch.setattr(mod, "_run", lambda args: (0, output))
    result = mod.inspect_systemd_unit()
    assert result["query_ok"] is True
    assert result["release_layout"] is True
    assert result["environment_files"] == "/etc/r2-relay/vps.env (ignore_errors=no)"
    assert "R2_RELAY_TOKEN" not in str(result)
    assert "Environment=" not in str(result)


def test_deployment_inspect_keeps_expected_443_8443_and_loopback_18085(monkeypatch):
    def fake(args):
        if args[0].endswith("systemctl"):
            return 0, "LoadState=loaded\nActiveState=active\n"
        return 0, (
            "LISTEN 0 128 0.0.0.0:443 0.0.0.0:*\n"
            "LISTEN 0 128 *:8443 *:*\n"
            "LISTEN 0 128 127.0.0.1:18085 0.0.0.0:*\n"
        )
    monkeypatch.setattr(mod, "_run", fake)
    monkeypatch.setattr(mod, "inspect_current_release",
                        lambda: {"status": "VALID", "commit": "d" * 40})
    monkeypatch.setattr(mod, "inspect_systemd_unit",
                        lambda: {"query_ok": True, "release_layout": True})
    result = mod.deployment_inspect()
    assert result["current_release"]["status"] == "VALID"
    assert result["listening_ports"]["443"] == ["0.0.0.0"]
    assert result["listening_ports"]["8443"] == ["*"]
    assert result["listening_ports"]["18085"] == ["127.0.0.1"]
    assert result["modified"] is False
    assert result["credentials_read"] is False
    assert result["environment_file_contents_read"] is False

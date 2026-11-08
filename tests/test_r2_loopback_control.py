from pathlib import Path
from types import SimpleNamespace
import importlib.util
import os
import stat
import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release_fixture(tmp_path, module, sha=None):
    sha = sha or "a" * 40
    releases = tmp_path / "releases"
    releases.mkdir(mode=0o750)
    release = releases / sha
    release.mkdir(mode=0o750)
    marker = release / ".deployment-commit"
    marker.write_text(sha + "\n")
    marker.chmod(0o640)
    deploy = release / "deploy"
    deploy.mkdir(mode=0o750)
    unit = deploy / "vps.service"
    unit.write_text("[Service]\nWorkingDirectory=/opt/r2-relay-current\n")
    unit.chmod(0o640)
    bindir = release / ".venv/bin"
    bindir.mkdir(parents=True, mode=0o750)
    uvicorn = bindir / "uvicorn"
    uvicorn.write_text("#!/bin/sh\n")
    uvicorn.chmod(0o750)
    current = tmp_path / "current"
    current.symlink_to(release)
    module.RELEASES = releases
    module.CURRENT = current
    return releases, release, current, unit, uvicorn, sha


def test_fixed_rootd_actions():
    daemon = load("rootd_check", "ops/r2_rootd.py")
    calls = []
    daemon.run = lambda argv, timeout=180: calls.append(argv) or {"exit_code": 0}
    for action in ("r2_service_verify", "r2_loopback_start"):
        assert daemon.process_request('{"action":"' + action + '"}', 12, 12)["exit_code"] == 0
    assert calls[0] == ["/usr/bin/python3", "/usr/local/libexec/r2_loopback_control.py"]
    assert calls[1] == calls[0] + ["--start"]
    assert "error" in daemon.process_request(
        '{"action":"r2_loopback_start","cmd":"sh"}', 12, 12)


def test_registration():
    module = load("mcp_check", "ops/generic_vps_mcp.py")
    class Fake:
        def __init__(self):
            self.names = []
        def tool(self):
            def decorator(fn):
                self.names.append(fn.__name__)
                return fn
            return decorator
    app = Fake()
    module.register(app)
    assert "vps_r2_service_verify" in app.names
    assert "vps_r2_loopback_start" in app.names
    assert module._r2_install_request("root_shell") == {"error": "action_denied"}


def test_verify_fails_for_non_root(monkeypatch):
    module = load("loop_a", "ops/r2_loopback_control.py")
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    with pytest.raises(ValueError, match="root_required"):
        module.verify()


def test_active_release_accepts_only_exact_release_and_marker(tmp_path):
    module = load("loop_release", "ops/r2_loopback_control.py")
    releases, release, current, unit, uvicorn, sha = release_fixture(tmp_path, module)
    resolved, commit, source, executable = module.active_release(
        os.getgid(), root_uid=os.geteuid())
    assert resolved == release
    assert commit == sha
    assert source == unit
    assert executable == uvicorn


def test_active_release_rejects_escape_and_bad_marker(tmp_path):
    module = load("loop_release_bad", "ops/r2_loopback_control.py")
    releases, release, current, unit, uvicorn, sha = release_fixture(tmp_path, module)
    outside = tmp_path / "outside"
    outside.mkdir()
    current.unlink()
    current.symlink_to(outside)
    with pytest.raises(ValueError, match="outside_release_root"):
        module.active_release(os.getgid(), root_uid=os.geteuid())
    current.unlink()
    current.symlink_to(release)
    (release / ".deployment-commit").write_text("f" * 40)
    with pytest.raises(ValueError, match="marker_mismatch"):
        module.active_release(os.getgid(), root_uid=os.geteuid())


def test_systemd_state_accepts_release_unit_and_real_port_contract(tmp_path, monkeypatch):
    module = load("loop_systemd", "ops/r2_loopback_control.py")
    releases, release, current, unit_source, uvicorn, sha = release_fixture(tmp_path, module)
    module.UNIT = tmp_path / "r2-relay-vps.service"
    module.UNIT.write_bytes(unit_source.read_bytes())
    module.CURRENT = Path("/opt/r2-relay-current")
    properties = {
        "FragmentPath": str(module.UNIT),
        "DropInPaths": "",
        "User": "r2relay",
        "Group": "r2relay",
        "WorkingDirectory": "/opt/r2-relay-current",
        "EnvironmentFiles": "/etc/r2-relay/vps.env (ignore_errors=no)",
        "ExecStart": "{ path=/opt/r2-relay-current/.venv/bin/uvicorn ; argv[]=/opt/r2-relay-current/.venv/bin/uvicorn --app-dir /opt/r2-relay-current/vps --host 127.0.0.1 --port 18085 }",
        "ActiveState": "active",
    }
    monkeypatch.setattr(module, "systemd_properties", lambda: properties)
    assert module.systemd_state(release) == "active"
    properties["WorkingDirectory"] = "/opt/r2-relay"
    with pytest.raises(ValueError, match="working_directory"):
        module.systemd_state(release)


def test_verify_accepts_release_layout_and_preserves_both_front_ports(tmp_path, monkeypatch):
    module = load("loop_verify", "ops/r2_loopback_control.py")
    env_dir = tmp_path / "etc-r2"
    env_dir.mkdir(mode=0o700)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    releases = tmp_path / "releases"
    releases.mkdir(mode=0o750)
    unit = tmp_path / "unit"
    unit.write_text("[Service]\n")
    module.ENV_DIR, module.CACHE, module.RELEASES, module.UNIT = env_dir, cache, releases, unit
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1001, pw_gid=1001))
    monkeypatch.setattr(module, "regular", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "active_release", lambda gid: (tmp_path / "release", "e" * 40, tmp_path / "source", tmp_path / "uvicorn"))
    monkeypatch.setattr(module, "parse_private_env", lambda: 8)
    monkeypatch.setattr(module, "systemd_state", lambda release: "active")
    monkeypatch.setattr(module, "listening", lambda port: port in (443, 8443))
    monkeypatch.setattr(module, "bound_addresses", lambda port: ["127.0.0.1:18085"] if port == 18085 else [])
    original_lstat = Path.lstat
    def fake_lstat(path):
        info = original_lstat(path)
        if path == env_dir:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_gid=0)
        if path == cache:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=1001, st_gid=1001)
        if path == releases:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_gid=1001)
        return info
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    result = module.verify()
    assert result["status"] == "SERVICE_VERIFY_PASS"
    assert result["release_commit"] == "e" * 40
    assert result["port_443_listening"] is True
    assert result["port_8443_listening"] is True
    assert result["port_18085_loopback_only"] is True
    assert result["token_emitted"] is False


def test_verify_rejects_missing_8443_listener(tmp_path, monkeypatch):
    module = load("loop_verify_port", "ops/r2_loopback_control.py")
    env_dir = tmp_path / "etc-r2"
    env_dir.mkdir(mode=0o700)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    releases = tmp_path / "releases"
    releases.mkdir(mode=0o750)
    unit = tmp_path / "unit"
    unit.write_text("[Service]\n")
    module.ENV_DIR, module.CACHE, module.RELEASES, module.UNIT = env_dir, cache, releases, unit
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1001, pw_gid=1001))
    monkeypatch.setattr(module, "regular", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "active_release", lambda gid: (tmp_path / "release", "e" * 40, tmp_path / "source", tmp_path / "uvicorn"))
    monkeypatch.setattr(module, "parse_private_env", lambda: 8)
    monkeypatch.setattr(module, "systemd_state", lambda release: "active")
    monkeypatch.setattr(module, "listening", lambda port: port == 443)
    monkeypatch.setattr(module, "bound_addresses", lambda port: ["127.0.0.1:18085"] if port == 18085 else [])
    original_lstat = Path.lstat
    def fake_lstat(path):
        info = original_lstat(path)
        if path == env_dir:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_gid=0)
        if path == cache:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=1001, st_gid=1001)
        if path == releases:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_gid=1001)
        return info
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(ValueError, match="listeners_missing"):
        module.verify()

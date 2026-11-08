import base64
import importlib.util
import io
from pathlib import Path
import struct
import subprocess
import shutil
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


receiver = load_module("r2relay_deploy_receiver", "ops/r2relay_deploy_receiver.py")
bootstrap = load_module("bootstrap_github_actions_deploy", "ops/bootstrap_github_actions_deploy.py")

SHA = "a" * 40


def make_archive(path: Path, *, marker=SHA, extra=None, unit=None):
    files = {
        "vps/pyproject.toml": b"[build-system]\n",
        "vps/r2relay_vps/app.py": b"app = True\n",
        "deploy/vps.service": (unit if unit is not None else receiver.UNIT_TEXT).encode(),
        "deploy/requirements-vps.lock": b"fastapi==0.162.2\n",
        ".deployment-commit": (marker + "\n").encode(),
    }
    with tarfile.open(path, "w:") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
        if extra:
            archive.addfile(extra)


def test_forced_command_accepts_only_exact_full_sha():
    assert receiver.parse_original_command("deploy " + SHA) == SHA
    for command in (
        None, "", "deploy main", "deploy " + "A" * 40,
        "deploy " + "a" * 39, "deploy " + SHA + " extra",
        "bash -lc id", "deploy " + SHA + "; id",
    ):
        try:
            receiver.parse_original_command(command)
        except receiver.DeployError:
            pass
        else:
            raise AssertionError(f"accepted unsafe command: {command!r}")


def test_valid_archive_and_commit_marker(tmp_path):
    archive = tmp_path / "release.tar"
    make_archive(archive)
    members, info = receiver.validate_archive(archive, SHA)
    assert info["files"] == 5
    assert info["unpacked_bytes"] > 0
    stage = tmp_path / "stage"
    receiver.extract_archive(archive, stage, members)
    assert (stage / "vps/r2relay_vps/app.py").read_bytes() == b"app = True\n"
    assert not (stage / ".deployment-commit").exists()


def test_archive_rejects_wrong_commit_marker(tmp_path):
    archive = tmp_path / "wrong.tar"
    make_archive(archive, marker="b" * 40)
    try:
        receiver.validate_archive(archive, SHA)
    except receiver.DeployError as exc:
        assert str(exc) == "commit_marker_mismatch"
    else:
        raise AssertionError("wrong commit marker accepted")


def test_archive_rejects_duplicate_member_names(tmp_path):
    archive = tmp_path / "duplicate.tar"
    make_archive(archive)
    with tarfile.open(archive, "a:") as tf:
        item = tarfile.TarInfo("vps/r2relay_vps/app.py")
        item.size = 1
        tf.addfile(item, io.BytesIO(b"x"))
    try:
        receiver.validate_archive(archive, SHA)
    except receiver.DeployError as exc:
        assert str(exc) == "archive_path_invalid"
    else:
        raise AssertionError("duplicate archive path accepted")


def test_archive_rejects_absolute_member_names(tmp_path):
    archive = tmp_path / "absolute.tar"
    make_archive(archive, extra=tarfile.TarInfo("/tmp/escape"))
    try:
        receiver.validate_archive(archive, SHA)
    except receiver.DeployError as exc:
        assert str(exc) == "archive_absolute_path"
    else:
        raise AssertionError("absolute archive path accepted")


def test_archive_rejects_path_traversal(tmp_path):
    archive = tmp_path / "traversal.tar"
    extra = tarfile.TarInfo("../escape")
    extra.size = 1
    with tarfile.open(archive, "w:") as tf:
        for name, data in {
            "vps/pyproject.toml": b"x",
            "vps/r2relay_vps/app.py": b"x",
            "deploy/vps.service": receiver.UNIT_TEXT.encode(),
            "deploy/requirements-vps.lock": b"x",
            ".deployment-commit": (SHA + "\n").encode(),
        }.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            tf.addfile(item, io.BytesIO(data))
        tf.addfile(extra, io.BytesIO(b"x"))
    try:
        receiver.validate_archive(archive, SHA)
    except receiver.DeployError as exc:
        assert str(exc) == "archive_path_invalid"
    else:
        raise AssertionError("path traversal accepted")


def test_archive_rejects_symlink_hardlink_and_device(tmp_path):
    for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE):
        archive = tmp_path / ("special-" + str(kind) + ".tar")
        special = tarfile.TarInfo("deploy/evil")
        special.type = kind
        special.linkname = "../outside"
        with tarfile.open(archive, "w:") as tf:
            for name, data in {
                "vps/pyproject.toml": b"x",
                "vps/r2relay_vps/app.py": b"x",
                "deploy/vps.service": receiver.UNIT_TEXT.encode(),
                "deploy/requirements-vps.lock": b"x",
                ".deployment-commit": (SHA + "\n").encode(),
            }.items():
                item = tarfile.TarInfo(name)
                item.size = len(data)
                tf.addfile(item, io.BytesIO(data))
            tf.addfile(special)
        try:
            receiver.validate_archive(archive, SHA)
        except receiver.DeployError as exc:
            assert str(exc) == "archive_special_file_denied"
        else:
            raise AssertionError(f"special tar member accepted: {kind}")


def test_archive_rejects_privileged_modes_and_changed_unit(tmp_path):
    archive = tmp_path / "privileged.tar"
    make_archive(archive)
    with tarfile.open(archive, "a:") as tf:
        item = tarfile.TarInfo("deploy/extra")
        item.size = 1
        item.mode = 0o4755
        tf.addfile(item, io.BytesIO(b"x"))
    try:
        receiver.validate_archive(archive, SHA)
    except receiver.DeployError as exc:
        assert str(exc) == "archive_privileged_mode_denied"
    else:
        raise AssertionError("setuid member accepted")

    changed = tmp_path / "changed-unit.tar"
    make_archive(changed, unit=receiver.UNIT_TEXT.replace("127.0.0.1", "0.0.0.0"))
    try:
        receiver.validate_archive(changed, SHA)
    except receiver.DeployError as exc:
        assert str(exc) == "service_template_not_approved"
    else:
        raise AssertionError("changed systemd unit accepted")


def test_current_symlink_is_atomic_and_fixed_target(tmp_path, monkeypatch):
    release_root = tmp_path / "releases"
    release_root.mkdir()
    target = release_root / SHA
    target.mkdir()
    current = tmp_path / "current"
    monkeypatch.setattr(receiver, "CURRENT", current)
    monkeypatch.setattr(receiver, "RELEASES", release_root)
    receiver.set_current(str(target))
    assert current.is_symlink()
    assert current.resolve() == target
    receiver.set_current(None)
    assert not current.exists()


def make_existing_release(path: Path, python_target: Path, gid: int):
    for directory in (
        path, path / ".venv", path / ".venv/bin",
        path / "vps", path / "vps/r2relay_vps",
    ):
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        directory.chmod(0o750)
    for name in ("python", "python3", "python3.11"):
        (path / ".venv/bin" / name).symlink_to(python_target)
    uvicorn = path / ".venv/bin/uvicorn"
    uvicorn.write_text("#!/bin/sh\\n")
    uvicorn.chmod(0o750)
    app = path / "vps/r2relay_vps/app.py"
    app.write_text("app = True\\n")
    app.chmod(0o640)


def test_existing_release_can_be_validated_for_rollback_reuse(tmp_path, monkeypatch):
    import os

    release = tmp_path / SHA
    python_target = Path(sys.executable).resolve()
    make_existing_release(release, python_target, os.getgid())
    monkeypatch.setattr(receiver, "run_as_r2relay", lambda argv, timeout=120: None)
    receiver.validate_existing_release(
        release, os.getgid(), owner_uid=os.getuid(), python_executable=python_target
    )


def test_existing_release_rejects_unexpected_symlink_and_writable_file(tmp_path, monkeypatch):
    import os

    release = tmp_path / SHA
    python_target = Path(sys.executable).resolve()
    make_existing_release(release, python_target, os.getgid())
    monkeypatch.setattr(receiver, "run_as_r2relay", lambda argv, timeout=120: None)

    evil = release / "vps/r2relay_vps/evil"
    evil.symlink_to("/etc/passwd")
    try:
        receiver.validate_existing_release(
            release, os.getgid(), owner_uid=os.getuid(), python_executable=python_target
        )
    except receiver.DeployError as exc:
        assert str(exc) == "existing_release_symlink_unexpected"
    else:
        raise AssertionError("unexpected release symlink accepted")
    evil.unlink()

    app = release / "vps/r2relay_vps/app.py"
    app.chmod(0o660)
    try:
        receiver.validate_existing_release(
            release, os.getgid(), owner_uid=os.getuid(), python_executable=python_target
        )
    except receiver.DeployError as exc:
        assert str(exc) == "existing_release_file_permissions_invalid"
    else:
        raise AssertionError("writable existing release file accepted")


def test_existing_release_rejects_python_symlink_outside_base_interpreter(tmp_path, monkeypatch):
    import os

    release = tmp_path / SHA
    make_existing_release(release, Path("/bin/sh"), os.getgid())
    monkeypatch.setattr(receiver, "run_as_r2relay", lambda argv, timeout=120: None)
    try:
        receiver.validate_existing_release(
            release, os.getgid(), owner_uid=os.getuid(), python_executable=Path(sys.executable).resolve()
        )
    except receiver.DeployError as exc:
        assert str(exc) == "existing_release_python_link_invalid"
    else:
        raise AssertionError("unexpected venv Python target accepted")


def test_bootstrap_accepts_only_valid_ed25519_public_key():
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + b"x" * 32
    key = base64.b64encode(blob).decode()
    encoded, comment, fingerprint = bootstrap.parse_public_key("ssh-ed25519 " + key + " test")
    assert encoded == key
    assert comment == "test"
    assert fingerprint.startswith("SHA256:")
    for invalid in ("ssh-rsa AAAA test", "ssh-ed25519 not-base64", "ssh-ed25519 " + key + "\nextra"):
        try:
            bootstrap.parse_public_key(invalid)
        except bootstrap.BootstrapError:
            pass
        else:
            raise AssertionError("invalid key accepted")


def test_bootstrap_detects_existing_key_without_rewriting():
    assert bootstrap.existing_key_data([
        'command="/usr/bin/sudo -n /usr/bin/python3 /usr/local/libexec/r2relay_deploy_receiver.py",restrict ssh-ed25519 AAAA comment'
    ]) == {"AAAA"}


def test_automatic_rollback_restores_unit_and_previous_release(tmp_path, monkeypatch):
    release_root = tmp_path / "releases"
    release_root.mkdir()
    previous = release_root / ("b" * 40)
    current_release = release_root / SHA
    previous.mkdir()
    current_release.mkdir()
    current = tmp_path / "current"
    current.symlink_to(current_release)
    unit = tmp_path / "r2-relay-vps.service"
    unit.write_text("new unit")
    backup = tmp_path / "backup.service"
    backup.write_text("old unit")

    monkeypatch.setattr(receiver, "CURRENT", current)
    monkeypatch.setattr(receiver, "RELEASES", release_root)
    monkeypatch.setattr(receiver, "UNIT", unit)
    monkeypatch.setattr(receiver, "run_fixed", lambda argv, timeout=120: None)
    monkeypatch.setattr(receiver, "health_check", lambda attempts=25: True)
    monkeypatch.setattr(receiver.os, "chown", lambda *args, **kwargs: None)

    assert receiver.restore(backup, str(previous), True) is True
    assert unit.read_text() == "old unit"
    assert current.is_symlink()
    assert current.resolve() == previous


def test_first_deploy_rollback_removes_new_pointer(tmp_path, monkeypatch):
    release_root = tmp_path / "releases"
    release_root.mkdir()
    current_release = release_root / SHA
    current_release.mkdir()
    current = tmp_path / "current"
    current.symlink_to(current_release)
    unit = tmp_path / "r2-relay-vps.service"
    unit.write_text("new unit")
    backup = tmp_path / "backup.service"
    backup.write_text("legacy unit")

    monkeypatch.setattr(receiver, "CURRENT", current)
    monkeypatch.setattr(receiver, "RELEASES", release_root)
    monkeypatch.setattr(receiver, "UNIT", unit)
    monkeypatch.setattr(receiver, "run_fixed", lambda argv, timeout=120: None)
    monkeypatch.setattr(receiver, "health_check", lambda attempts=25: True)
    monkeypatch.setattr(receiver.os, "chown", lambda *args, **kwargs: None)

    assert receiver.restore(backup, "", False) is True
    assert unit.read_text() == "legacy unit"
    assert not current.exists()
    assert not current.is_symlink()


def test_production_workflow_is_owner_gated_and_requires_deployment_checks():
    workflow = (ROOT / ".github/workflows/deploy-production.yml").read_text(encoding="utf-8")
    assert "issue_comment:" in workflow
    assert "github.event.issue.number == 41" in workflow
    assert "github.event.issue.pull_request == null" in workflow
    assert "github.event.comment.user.login == 'sunxu'" in workflow
    assert 're.fullmatch(r"/deploy ([0-9a-f]{40})"' in workflow
    assert "git merge-base --is-ancestor" in workflow
    assert 'select(.name == "deploy-tests"' in workflow
    assert '.app.slug == "github-actions"' in workflow
    assert '[ "$conclusion" = "success" ]' in workflow
    assert "StrictHostKeyChecking=yes" in workflow
    assert "VPS_HOST: 104.224.168.192" in workflow
    assert "VPS_USER: r2relay-deploy" in workflow
    assert "VPS_USER: root" not in workflow
    assert "jq -s --arg sha" in workflow
    assert r"IFS=$'\\t'" not in workflow
    assert 'result.get("commit") != expected_sha' in workflow
    assert "SSH_OR_RECEIVER_TRANSPORT_FAILED" in workflow
    assert "workflow_dispatch:" not in workflow
    assert "push:" not in workflow


def test_bootstrap_accepts_only_current_or_exact_legacy_sudoers_policy():
    assert bootstrap.is_managed_sudoers_policy(bootstrap.SUDOERS_TEXT)
    assert bootstrap.is_managed_sudoers_policy(bootstrap.LEGACY_SUDOERS_TEXT)
    assert "/usr/bin/python3 /usr/local/libexec/r2relay_deploy_receiver.py" in bootstrap.LEGACY_SUDOERS_TEXT
    assert not bootstrap.is_managed_sudoers_policy(
        bootstrap.LEGACY_SUDOERS_TEXT + "r2relay-deploy ALL=(root) NOPASSWD: ALL\n"
    )
    assert not bootstrap.is_managed_sudoers_policy("")


def test_bootstrap_uses_a_dedicated_account_and_one_sudo_command():
    assert bootstrap.DEPLOY_USER == "r2relay-deploy"
    assert bootstrap.FORCED_COMMAND.startswith("/usr/bin/sudo -n ")
    assert 'env_keep += "SSH_ORIGINAL_COMMAND"' in bootstrap.SUDOERS_TEXT
    assert bootstrap.SUDO_COMMAND == "/usr/local/libexec/r2relay_deploy_receiver.py"
    assert bootstrap.FORCED_COMMAND == "/usr/bin/sudo -n " + bootstrap.SUDO_COMMAND
    assert "NOPASSWD: " + bootstrap.SUDO_COMMAND + ' ""' in bootstrap.SUDOERS_TEXT
    assert "/usr/bin/python3" not in bootstrap.SUDOERS_TEXT
    assert "ALL=(ALL)" not in bootstrap.SUDOERS_TEXT
    line = bootstrap.authorized_key_line("AAAA", "test")
    assert line.startswith('command="/usr/bin/sudo -n /usr/local/libexec/r2relay_deploy_receiver.py",restrict ')
    assert ',restrict ssh-ed25519 AAAA test' in line


def test_bootstrap_reports_only_password_lock_state(tmp_path):
    shadow = tmp_path / "shadow"
    shadow.write_text("r2relay-deploy:!locked-hash:20000:0:99999:7:::\\n")
    assert bootstrap.password_is_locked("r2relay-deploy", shadow) is True
    shadow.write_text("r2relay-deploy:$6$secret-hash:20000:0:99999:7:::\\n")
    assert bootstrap.password_is_locked("r2relay-deploy", shadow) is False
    shadow.write_text("another-user:!:20000:0:99999:7:::\\n")
    try:
        bootstrap.password_is_locked("r2relay-deploy", shadow)
    except bootstrap.BootstrapError as exc:
        assert str(exc) == "shadow_account_missing"
    else:
        raise AssertionError("missing shadow account accepted")


def test_sudoers_policy_is_valid_when_visudo_is_available(tmp_path):
    visudo = shutil.which("visudo")
    sudo = shutil.which("sudo")
    if sys.platform == "darwin":
        return
    assert visudo and sudo, "Linux CI must provide visudo and sudo"
    policy = tmp_path / "r2-relay-deploy"
    policy.write_text(bootstrap.SUDOERS_TEXT, encoding="utf-8")
    policy.chmod(0o440)
    owner = subprocess.run(
        [sudo, "chown", "root:root", str(policy)],
        capture_output=True, text=True, check=False,
    )
    assert owner.returncode == 0, owner.stderr
    result = subprocess.run(
        [sudo, visudo, "-cf", str(policy)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_deployment_lock_requires_root_owned_private_regular_file(tmp_path, monkeypatch):
    import os
    import stat

    lock_path = tmp_path / "deploy.lock"
    monkeypatch.setattr(receiver, "LOCK", lock_path)
    monkeypatch.setattr(receiver.os, "fstat", lambda _fd: os.stat_result(
        (stat.S_IFREG | 0o600, 1, 1, 1, 0, 0, 0, 0, 0, 0)
    ))
    fd = receiver.open_deploy_lock()
    receiver.os.close(fd)

    monkeypatch.setattr(receiver.os, "fstat", lambda _fd: os.stat_result(
        (stat.S_IFREG | 0o644, 1, 1, 1, 0, 0, 0, 0, 0, 0)
    ))
    try:
        receiver.open_deploy_lock()
    except receiver.DeployError as exc:
        assert str(exc) == "deployment_lock_file_unsafe"
    else:
        raise AssertionError("permissive lock file accepted")


def test_deployment_tests_run_on_main_push():
    workflow = (ROOT / ".github/workflows/deploy-tests.yml").read_text(encoding="utf-8")
    assert "push:" in workflow
    assert "branches: [main]" in workflow
    assert "pull_request:" in workflow

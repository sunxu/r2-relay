import base64
import importlib.util
import io
from pathlib import Path
import struct
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
        'command="/usr/bin/python3 /usr/local/libexec/r2relay_deploy_receiver.py",restrict ssh-ed25519 AAAA comment'
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
    assert 'select(.name == "deploy-tests")' in workflow
    assert "StrictHostKeyChecking=yes" in workflow
    assert "VPS_HOST: 104.224.168.192" in workflow
    assert "workflow_dispatch:" not in workflow
    assert "push:" not in workflow


def test_deployment_tests_run_on_main_push():
    workflow = (ROOT / ".github/workflows/deploy-tests.yml").read_text(encoding="utf-8")
    assert "push:" in workflow
    assert "branches: [main]" in workflow
    assert "pull_request:" in workflow

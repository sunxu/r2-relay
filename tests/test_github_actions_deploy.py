import base64
import importlib.util
import io
import os
import stat
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


def make_archive(path: Path, *, marker=SHA, extra=None, unit=None, include_config_marker=True):
    files = {
        "vps/pyproject.toml": b"[build-system]\n",
        "vps/r2relay_vps/app.py": b"app = True\n",
        "deploy/vps.service": (unit if unit is not None else receiver.UNIT_TEXT).encode(),
        "deploy/requirements-vps.lock": b"fastapi==0.162.2\n",
        ".deployment-commit": (marker + "\n").encode(),
    }
    if include_config_marker:
        files["deploy/download-config-dropin-v1"] = b"r2-relay-download-config-dropin-v1\n"
    with tarfile.open(path, "w:") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
        if extra:
            archive.addfile(extra)



def test_receiver_build_id_is_in_every_emitted_receipt(capsys):
    receiver.emit("TEST", marker="ok")
    import json
    payload = json.loads(capsys.readouterr().out)
    assert payload["receiver_build_id"] == "r2-relay-deploy-receiver-v7-leased-real-r2-test"
    assert payload["marker"] == "ok"


def test_runtime_user_failure_identifies_step_without_leaking_stderr(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(
        receiver.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="private-index-credential"),
    )
    try:
        receiver.run_as_r2relay(["/venv/bin/pip", "install"], step="pip_project")
    except receiver.DeployError as exc:
        assert str(exc) == "runtime_user_command_failed:pip_project:exit_1"
        assert "private-index-credential" not in str(exc)
    else:
        raise AssertionError("failed runtime command was accepted")

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


def test_deployment_staging_path_is_unique_and_created_by_extractor(tmp_path, monkeypatch):
    monkeypatch.setattr(receiver, "RELEASES", tmp_path)
    monkeypatch.setattr(receiver.secrets, "token_hex", lambda _n: "f" * 16)
    stage = receiver.new_staging_path(SHA)
    assert stage == tmp_path / (".staging-" + SHA + "-" + "f" * 16)
    assert not stage.exists()
    archive = tmp_path / "stage-contract.tar"
    make_archive(archive)
    members, _ = receiver.validate_archive(archive, SHA)
    receiver.extract_archive(archive, stage, members, SHA)
    assert stage.is_dir()
    assert (stage / "vps/r2relay_vps/app.py").is_file()
    assert (stage / ".deployment-commit").read_text(encoding="ascii") == SHA + "\n"

def test_valid_archive_and_commit_marker(tmp_path):
    archive = tmp_path / "release.tar"
    make_archive(archive)
    members, info = receiver.validate_archive(archive, SHA)
    assert info["files"] == 6
    assert info["unpacked_bytes"] > 0
    stage = tmp_path / "stage"
    receiver.extract_archive(archive, stage, members, SHA)
    assert (stage / "vps/r2relay_vps/app.py").read_bytes() == b"app = True\n"
    marker = stage / ".deployment-commit"
    assert marker.read_text(encoding="ascii") == SHA + "\n"
    assert marker.stat().st_mode & 0o777 == 0o640


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
        path, path / ".venv", path / ".venv/bin", path / ".venv/lib",
        path / "vps", path / "vps/r2relay_vps",
    ):
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        directory.chmod(0o750)
    for name in ("python", "python3", "python3.11"):
        (path / ".venv/bin" / name).symlink_to(python_target)
    (path / ".venv/lib64").symlink_to("lib")
    uvicorn = path / ".venv/bin/uvicorn"
    uvicorn.write_text("#!/bin/sh\n")
    uvicorn.chmod(0o750)
    app = path / "vps/r2relay_vps/app.py"
    app.write_text("app = True\n")
    app.chmod(0o640)


def test_chown_release_allows_only_local_venv_lib64_symlink(tmp_path, monkeypatch):
    import os

    root = tmp_path / "release"
    lib = root / ".venv/lib"
    lib.mkdir(parents=True)
    lib64 = root / ".venv/lib64"
    lib64.symlink_to("lib")
    monkeypatch.setattr(receiver.os, "chown", lambda *args, **kwargs: None)

    receiver.chown_release(root, 4242)
    assert lib64.is_symlink()
    assert os.readlink(lib64) == "lib"
    assert lib64.resolve() == lib.resolve()

    evil = root / "unexpected"
    evil.symlink_to(lib, target_is_directory=True)
    try:
        receiver.chown_release(root, 4242)
    except receiver.DeployError as exc:
        assert str(exc) == "release_symlink_unexpected"
    else:
        raise AssertionError("unexpected directory symlink accepted")


def test_existing_release_accepts_only_local_venv_lib64_symlink(tmp_path, monkeypatch):
    import os

    release = tmp_path / SHA
    python_target = Path(sys.executable).resolve()
    make_existing_release(release, python_target, os.getgid())
    monkeypatch.setattr(receiver, "run_as_r2relay", lambda argv, timeout=120, **kwargs: None)
    receiver.validate_existing_release(
        release, os.getgid(), owner_uid=os.getuid(), python_executable=python_target
    )

    evil = release / ".venv/evil-dir"
    evil.symlink_to(release / ".venv/lib", target_is_directory=True)
    try:
        receiver.validate_existing_release(
            release, os.getgid(), owner_uid=os.getuid(), python_executable=python_target
        )
    except receiver.DeployError as exc:
        assert str(exc) == "existing_release_directory_invalid"
    else:
        raise AssertionError("unexpected venv directory symlink accepted")

def test_chown_release_keeps_group_readable_immutable_source_modes(tmp_path, monkeypatch):
    import os
    import stat

    root = tmp_path / "release"
    source = root / "vps"
    source.mkdir(parents=True)
    regular = source / "app.py"
    executable = source / "launcher"
    regular.write_text("app = True\n")
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    chown_calls = []
    monkeypatch.setattr(receiver.os, "chown", lambda path, uid, gid, **kwargs: chown_calls.append((Path(path), uid, gid)))

    receiver.chown_release(root, 4242)

    assert stat.S_IMODE(regular.stat().st_mode) == 0o640
    assert stat.S_IMODE(executable.stat().st_mode) == 0o750
    assert stat.S_IMODE(source.stat().st_mode) == 0o750
    assert stat.S_IMODE(root.stat().st_mode) == 0o750
    assert chown_calls
    assert all(uid == 0 and gid == 4242 for _, uid, gid in chown_calls)
    assert stat.S_IMODE(regular.stat().st_mode) & 0o040
    assert not (stat.S_IMODE(regular.stat().st_mode) & 0o020)

def test_prepare_release_builds_from_writable_copy_and_keeps_release_read_only(tmp_path, monkeypatch):
    import os
    import stat

    stage = tmp_path / "stage"
    source = stage / "vps"
    package = source / "r2relay_vps"
    package.mkdir(parents=True)
    (source / "pyproject.toml").write_text("[build-system]\nrequires = ['setuptools']\nbuild-backend = 'setuptools.build_meta'\n")
    (package / "app.py").write_text("app = True\n")
    (stage / "deploy").mkdir()
    (stage / "deploy/requirements-vps.lock").write_text("fastapi==0.162.2\n")
    for directory in (source, package):
        directory.chmod(0o550)
    for file in (source / "pyproject.toml", package / "app.py"):
        file.chmod(0o440)

    monkeypatch.setattr(receiver, "chown_release", lambda _root, _gid: None)
    local_build_sources = []

    def fake_run_as_r2relay(argv, timeout=120, *, step="runtime_user_command"):
        if len(argv) > 2 and argv[1] == "install" and Path(argv[-1]).name == "vps":
            build_source = Path(argv[-1])
            local_build_sources.append(build_source)
            assert build_source != source
            assert (build_source / "pyproject.toml").is_file()
            assert build_source.stat().st_uid == os.getuid()
            assert stat.S_IMODE(build_source.stat().st_mode) == 0o700
            assert stat.S_IMODE((build_source / "pyproject.toml").stat().st_mode) == 0o600

    monkeypatch.setattr(receiver, "run_as_r2relay", fake_run_as_r2relay)
    receiver.prepare_release(stage, os.getuid(), os.getgid())

    assert len(local_build_sources) == 1
    assert not local_build_sources[0].exists()
    assert not (source / "build").exists()
    assert stat.S_IMODE(source.stat().st_mode) == 0o550
    assert stat.S_IMODE((source / "pyproject.toml").stat().st_mode) == 0o440


def test_existing_release_can_be_validated_for_rollback_reuse(tmp_path, monkeypatch):
    import os

    release = tmp_path / SHA
    python_target = Path(sys.executable).resolve()
    make_existing_release(release, python_target, os.getgid())
    monkeypatch.setattr(receiver, "run_as_r2relay", lambda argv, timeout=120, **kwargs: None)
    receiver.validate_existing_release(
        release, os.getgid(), owner_uid=os.getuid(), python_executable=python_target
    )


def test_existing_release_rejects_unexpected_symlink_and_writable_file(tmp_path, monkeypatch):
    import os

    release = tmp_path / SHA
    python_target = Path(sys.executable).resolve()
    make_existing_release(release, python_target, os.getgid())
    monkeypatch.setattr(receiver, "run_as_r2relay", lambda argv, timeout=120, **kwargs: None)

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
    monkeypatch.setattr(receiver, "run_as_r2relay", lambda argv, timeout=120, **kwargs: None)
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
    monkeypatch.setattr(receiver, "run_fixed", lambda argv, timeout=120, **kwargs: None)
    monkeypatch.setattr(receiver, "health_check", lambda attempts=25, expected_download_enabled=None: True)
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
    monkeypatch.setattr(receiver, "run_fixed", lambda argv, timeout=120, **kwargs: None)
    monkeypatch.setattr(receiver, "health_check", lambda attempts=25, expected_download_enabled=None: True)
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
    assert "github.event.issue.number == 63" in workflow
    assert 'r"/download-config (disabled|real-test) ([0-9a-f]{40})"' in workflow
    assert 'result.get("profile") == "real-r2-test-v1"' in workflow
    assert 'gh issue comment "$ISSUE_NUMBER"' in workflow
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


def test_bootstrap_upgrades_only_the_exact_legacy_forced_command_key():
    encoded = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
    legacy = (
        'command="' + bootstrap.LEGACY_FORCED_COMMAND
        + '",restrict ssh-ed25519 ' + encoded + " r2-relay-github-actions\n"
    ).encode()
    updated, exists = bootstrap.update_authorized_keys(
        legacy, encoded, "r2-relay-github-actions"
    )
    assert exists is True
    assert updated.decode() == bootstrap.authorized_key_line(
        encoded, "r2-relay-github-actions"
    ) + "\n"
    repeated, exists_again = bootstrap.update_authorized_keys(
        updated, encoded, "r2-relay-github-actions"
    )
    assert exists_again is True
    assert repeated == updated


def test_bootstrap_rejects_unknown_authorized_key_options():
    encoded = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
    unknown = (
        'command="/bin/sh",restrict ssh-ed25519 '
        + encoded + " r2-relay-github-actions\n"
    ).encode()
    try:
        bootstrap.update_authorized_keys(unknown, encoded, "r2-relay-github-actions")
    except bootstrap.BootstrapError as exc:
        assert str(exc) == "unexpected_deploy_account_authorized_key"
    else:
        raise AssertionError("unknown authorized_keys command accepted")


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
    shadow.write_text("r2relay-deploy:!locked-hash:20000:0:99999:7:::\n")
    assert bootstrap.password_is_locked("r2relay-deploy", shadow) is True
    shadow.write_text("r2relay-deploy:$6$secret-hash:20000:0:99999:7:::\n")
    assert bootstrap.password_is_locked("r2relay-deploy", shadow) is False
    shadow.write_text("another-user:!:20000:0:99999:7:::\n")
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


def test_deployment_receipt_does_not_use_backtick_command_substitution():
    workflow = (ROOT / ".github/workflows/deploy-production.yml").read_text(encoding="utf-8")
    receipt = workflow.split("      - name: Publish deployment receipt", 1)[1]
    assert "printf -v body" in receipt
    assert "Requested Commit: %s" in receipt
    assert "`$TARGET_SHA`" not in receipt
    assert "`$receipt`" not in receipt
    assert 'receipt="No receiver/SSH response was produced; inspect the workflow logs for the failing step."' in receipt

def test_deployment_tests_run_on_main_push():
    workflow = (ROOT / ".github/workflows/deploy-tests.yml").read_text(encoding="utf-8")
    assert "push:" in workflow
    assert "branches: [main]" in workflow
    assert "pull_request:" in workflow



def test_download_config_command_accepts_only_disabled_or_real_test_and_full_sha():
    assert receiver.parse_download_config_command("download-config disabled " + SHA) == ("disabled", SHA)
    assert receiver.parse_download_config_command("download-config real-test " + SHA) == ("real-test", SHA)
    for command in (
        None,
        "",
        "download-config enabled " + SHA,
        "download-config production " + SHA,
        "download-config real-test main",
        "download-config disabled main",
        "download-config real-test " + "A" * 40,
        "download-config disabled " + "A" * 40,
        "download-config real-test " + SHA + " extra",
        "download-config disabled " + SHA + "; id",
        "deploy " + SHA,
    ):
        try:
            receiver.parse_download_config_command(command)
        except receiver.DeployError:
            pass
        else:
            raise AssertionError(f"accepted unsafe download config command: {command!r}")


def _prepare_download_config_receiver_test(tmp_path, monkeypatch):
    release_root = tmp_path / "releases"
    release = release_root / SHA
    release.mkdir(parents=True)
    (release / ".deployment-commit").write_text(SHA + "\n", encoding="ascii")
    marker = release / "deploy/download-config-dropin-v1"
    marker.parent.mkdir(parents=True)
    marker.write_text(receiver.DOWNLOAD_CONFIG_MARKER + "\n", encoding="ascii")

    current = tmp_path / "current"
    current.symlink_to(release)
    config_parent = tmp_path / "etc-r2-relay-download"
    config_parent.mkdir(mode=0o710, parents=True)
    config_path = config_parent / "download.toml"
    backup_parent = tmp_path / "var/backups/r2-relay"
    backup_parent.mkdir(parents=True)
    backup_root = backup_parent / "download-config"
    backup_root.mkdir(mode=0o700)
    lock_path = tmp_path / "deploy.lock"
    state_dir = tmp_path / "var/lib/r2-relay-deploy"
    state_dir.mkdir(mode=0o700, parents=True)

    monkeypatch.setattr(receiver, "DOWNLOAD_TEST_STATE_DIR", state_dir)
    monkeypatch.setattr(receiver, "DOWNLOAD_TEST_LEASE", state_dir / "download-test-lease.json")
    monkeypatch.setattr(receiver, "CURRENT", current)
    monkeypatch.setattr(receiver, "RELEASES", release_root)
    monkeypatch.setattr(receiver, "DOWNLOAD_CONFIG", config_path)
    monkeypatch.setattr(receiver, "DOWNLOAD_CONFIG_BACKUPS", backup_root)
    monkeypatch.setattr(receiver, "open_deploy_lock", lambda: os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600))
    monkeypatch.setattr(receiver, "validate_host_state", lambda **_kwargs: (os.getuid(), os.getgid(), str(release)))
    monkeypatch.setattr(receiver.os, "geteuid", lambda: 0)
    monkeypatch.setattr(receiver.os, "chown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(receiver.os, "fchown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(receiver, "_validate_download_config_paths", lambda _gid: None)
    monkeypatch.setattr(receiver, "_ensure_download_test_state_dir", lambda: None)

    real_lstat = Path.lstat

    def fake_lstat(path):
        if path == backup_root:
            return type("RootOwnedStat", (), {
                "st_mode": stat.S_IFDIR | 0o700,
                "st_uid": 0,
                "st_gid": 0,
            })()
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    return release, config_path, backup_root


def test_apply_download_config_disabled_writes_fixed_profile_and_restarts_once(tmp_path, monkeypatch):
    release, config_path, backup_root = _prepare_download_config_receiver_test(tmp_path, monkeypatch)
    restarts = []
    monkeypatch.setattr(receiver, "run_fixed", lambda argv, timeout=120: restarts.append(argv))
    monkeypatch.setattr(receiver, "health_check", lambda attempts=25, expected_download_enabled=None: True)

    result = receiver.apply_download_config_disabled(SHA)

    assert result["status"] == "DOWNLOAD_CONFIG_APPLIED"
    assert result["commit"] == SHA
    assert result["download_enabled"] is False
    assert result["config_path"] == str(config_path)
    assert result["config_mode"] == "0640"
    assert result["cache_modified"] is False
    assert result["environment_modified"] is False
    assert result["caddy_modified"] is False
    assert len(restarts) == 1
    assert restarts[0] == ["/usr/bin/systemctl", "restart", receiver.SERVICE]
    content = config_path.read_text(encoding="utf-8")
    assert "enabled = false" in content
    assert "part_size_mib = 16" in content
    assert "split_threshold_mib = 24" in content
    assert "max_cache_bytes = 0" in content
    assert config_path.stat().st_mode & 0o777 == 0o640
    assert list(backup_root.iterdir())


def test_apply_download_config_disabled_is_idempotent_without_restart(tmp_path, monkeypatch):
    _release, config_path, _backup_root = _prepare_download_config_receiver_test(tmp_path, monkeypatch)
    config_path.write_text(receiver.DOWNLOAD_CONFIG_TEXT, encoding="utf-8")
    restarts = []
    monkeypatch.setattr(receiver, "run_fixed", lambda argv, timeout=120: restarts.append(argv))
    monkeypatch.setattr(receiver, "health_check", lambda attempts=25, expected_download_enabled=None: True)

    result = receiver.apply_download_config_disabled(SHA)

    assert result["status"] == "DOWNLOAD_CONFIG_ALREADY_APPLIED"
    assert result["download_enabled"] is False
    assert result["service_restarted"] is False
    assert restarts == []


def test_apply_download_config_disabled_rolls_back_file_after_failed_health(tmp_path, monkeypatch):
    _release, config_path, _backup_root = _prepare_download_config_receiver_test(tmp_path, monkeypatch)
    previous = b"[download]\nenabled = false\n# owner-managed settings\n"
    config_path.write_bytes(previous)
    config_path.chmod(0o600)
    health_results = iter([False, True])
    restarts = []
    monkeypatch.setattr(receiver, "run_fixed", lambda argv, timeout=120: restarts.append(argv))
    monkeypatch.setattr(receiver, "health_check", lambda attempts=25, expected_download_enabled=None: next(health_results))

    try:
        receiver.apply_download_config_disabled(SHA)
    except receiver.DeployError as exc:
        assert str(exc) == "download_config_disable_failed_but_disabled:DeployError"
    else:
        raise AssertionError("failed config health check was accepted")

    assert config_path.read_bytes() == receiver.DOWNLOAD_CONFIG_TEXT.encode("utf-8")
    assert config_path.stat().st_mode & 0o777 == 0o640
    assert len(restarts) == 2


def test_apply_download_config_real_test_uses_fixed_bounded_profile_and_lease(tmp_path, monkeypatch):
    release, config_path, backup_root = _prepare_download_config_receiver_test(tmp_path, monkeypatch)
    config_path.write_text(receiver.DOWNLOAD_CONFIG_TEXT, encoding="utf-8")
    restarts = []
    scheduled = []
    monkeypatch.setattr(receiver, "run_fixed", lambda argv, timeout=120: (scheduled if "systemd-run" in argv[0] else restarts).append(argv))
    monkeypatch.setattr(receiver, "health_check", lambda attempts=25, expected_download_enabled=None: expected_download_enabled is False or expected_download_enabled is True)
    monkeypatch.setattr(receiver.secrets, "token_hex", lambda _n: "c" * 32)

    result = receiver.apply_download_config_real_test(SHA)

    assert result["status"] == "DOWNLOAD_TEST_ENABLED"
    assert result["download_enabled"] is True
    assert result["profile"] == "real-r2-test-v1"
    assert result["max_cache_bytes"] == 64 * 1024 * 1024
    assert result["min_free_bytes"] == 5 * 1024 * 1024 * 1024
    assert result["max_active_tasks"] == 1
    assert result["lease_minutes"] == 30
    assert "enabled = true" in config_path.read_text(encoding="utf-8")
    assert "max_cache_bytes = 67108864" in config_path.read_text(encoding="utf-8")
    assert "max_active_tasks = 1" in config_path.read_text(encoding="utf-8")
    assert receiver.DOWNLOAD_TEST_LEASE.is_file()
    assert receiver.DOWNLOAD_TEST_LEASE.stat().st_mode & 0o777 == 0o600
    assert len(scheduled) == 1
    assert "--timer-property=Persistent=true" in scheduled[0]
    assert "--on-calendar=" in " ".join(scheduled[0])
    assert len(restarts) == 1
    assert restarts[0] == ["/usr/bin/systemctl", "restart", receiver.SERVICE]
    assert list(backup_root.iterdir())


def test_apply_download_config_real_test_requires_active_release_and_disabled_profile(tmp_path, monkeypatch):
    _release, config_path, _backup_root = _prepare_download_config_receiver_test(tmp_path, monkeypatch)
    config_path.write_text("[download]\\nenabled = true\\n", encoding="utf-8")
    monkeypatch.setattr(receiver, "run_fixed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(receiver, "health_check", lambda *args, **kwargs: True)
    try:
        receiver.apply_download_config_real_test(SHA)
    except receiver.DeployError as exc:
        assert str(exc) == "download_test_requires_fixed_disabled_profile"
    else:
        raise AssertionError("real-test profile replaced a non-default configuration")


def test_apply_download_config_real_test_failures_leave_download_disabled(tmp_path, monkeypatch):
    _release, config_path, _backup_root = _prepare_download_config_receiver_test(tmp_path, monkeypatch)
    config_path.write_text(receiver.DOWNLOAD_CONFIG_TEXT, encoding="utf-8")
    health_results = iter([True, False, True])
    restarts = []
    def run(argv, timeout=120):
        restarts.append(argv)
    monkeypatch.setattr(receiver, "run_fixed", run)
    monkeypatch.setattr(receiver, "health_check", lambda attempts=25, expected_download_enabled=None: next(health_results, True))

    try:
        receiver.apply_download_config_real_test(SHA)
    except receiver.DeployError as exc:
        assert str(exc) == "download_test_enable_failed_disabled:DeployError"
    else:
        raise AssertionError("failed test enable was accepted")

    assert config_path.read_bytes() == receiver.DOWNLOAD_CONFIG_TEXT.encode("utf-8")
    assert not receiver.DOWNLOAD_TEST_LEASE.exists()
    assert restarts[-1] == ["/usr/bin/systemctl", "restart", receiver.SERVICE]


def test_download_test_expiry_disables_only_matching_expired_lease(tmp_path, monkeypatch):
    _release, config_path, _backup_root = _prepare_download_config_receiver_test(tmp_path, monkeypatch)
    lease_id = "d" * 32
    config_path.write_text(receiver.DOWNLOAD_TEST_PROFILE_TEXT, encoding="utf-8")
    receiver.DOWNLOAD_TEST_LEASE.write_text(
        __import__("json").dumps({"lease_id": lease_id, "expires_at": 1, "commit": SHA}) + "\\n",
        encoding="utf-8",
    )
    receiver.DOWNLOAD_TEST_LEASE.chmod(0o600)
    real_lstat = Path.lstat
    def fake_lstat(path):
        info = real_lstat(path)
        if path == receiver.DOWNLOAD_TEST_LEASE:
            return type("RootOwnedStat", (), {
                "st_mode": stat.S_IFREG | 0o600, "st_uid": 0, "st_nlink": 1,
            })()
        return info
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    restarts = []
    monkeypatch.setattr(receiver, "run_fixed", lambda argv, timeout=120: restarts.append(argv))
    monkeypatch.setattr(receiver, "health_check", lambda attempts=25, expected_download_enabled=None: expected_download_enabled is False)

    result = receiver.expire_download_test(lease_id)

    assert result["status"] == "DOWNLOAD_TEST_AUTO_DISABLED"
    assert result["download_enabled"] is False
    assert config_path.read_bytes() == receiver.DOWNLOAD_CONFIG_TEXT.encode("utf-8")
    assert not receiver.DOWNLOAD_TEST_LEASE.exists()
    assert restarts == [["/usr/bin/systemctl", "restart", receiver.SERVICE]]


def test_apply_download_config_disabled_requires_healthy_managed_host(tmp_path, monkeypatch):
    _release, config_path, _backup_root = _prepare_download_config_receiver_test(tmp_path, monkeypatch)
    monkeypatch.setattr(
        receiver, "validate_host_state",
        lambda **_kwargs: (_ for _ in ()).throw(receiver.DeployError("service_not_active_before_deploy")),
    )
    try:
        receiver.apply_download_config_disabled(SHA)
    except receiver.DeployError as exc:
        assert str(exc) == "service_not_active_before_deploy"
    else:
        raise AssertionError("download config applied without validated active host state")
    assert not config_path.exists()


def test_legacy_release_archive_without_download_marker_remains_deployable(tmp_path):
    archive = tmp_path / "legacy-release.tar"
    make_archive(archive, include_config_marker=False)

    members, info = receiver.validate_archive(archive, SHA)

    assert info["files"] == 5
    assert "deploy/download-config-dropin-v1" not in {member.name for member in members}


def test_archive_rejects_invalid_download_config_capability_marker(tmp_path):
    import io
    import tarfile

    archive = tmp_path / "invalid-config-marker.tar"
    files = {
        "vps/pyproject.toml": b"[build-system]\n",
        "vps/r2relay_vps/app.py": b"app = True\n",
        "deploy/vps.service": receiver.UNIT_TEXT.encode(),
        "deploy/requirements-vps.lock": b"fastapi==0.162.2\n",
        "deploy/download-config-dropin-v1": b"not-the-approved-marker\n",
        ".deployment-commit": (SHA + "\n").encode(),
    }
    with tarfile.open(archive, "w:") as output:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            output.addfile(info, io.BytesIO(data))

    try:
        receiver.validate_archive(archive, SHA)
    except receiver.DeployError as exc:
        assert str(exc) == "download_config_capability_marker_invalid"
    else:
        raise AssertionError("invalid download config marker accepted")



def test_health_check_can_require_download_disabled(monkeypatch):
    from types import SimpleNamespace

    responses = [
        b'{"status":"ok"}',
        b'{"status":"ok","download_enabled":true}',
        b'{"status":"ok","download_enabled":false}',
    ]

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return responses.pop(0)

    monkeypatch.setattr(receiver, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(receiver.time, "sleep", lambda _seconds: None)

    assert receiver.health_check(attempts=1) is True
    assert receiver.health_check(attempts=1, expected_download_enabled=False) is False
    assert receiver.health_check(attempts=1, expected_download_enabled=False) is True



def _prepare_download_config_path_validation(tmp_path, monkeypatch, *, existing_mode=None):
    from types import SimpleNamespace

    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()
    config_dir = etc_dir / "r2-relay-download"
    if existing_mode is not None:
        config_dir.mkdir(mode=existing_mode)
        config_dir.chmod(existing_mode)
    backup_parent = tmp_path / "var" / "backups" / "r2-relay"
    backup_parent.mkdir(parents=True)
    backup_root = backup_parent / "download-config"
    gid = 4242

    monkeypatch.setattr(receiver, "DOWNLOAD_CONFIG_DIR", config_dir)
    monkeypatch.setattr(receiver, "DOWNLOAD_CONFIG", config_dir / "download.toml")
    monkeypatch.setattr(receiver, "DOWNLOAD_CONFIG_BACKUPS", backup_root)
    monkeypatch.setattr(receiver.os, "chown", lambda *_args, **_kwargs: None)

    real_stat = Path.stat

    def fake_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if path == etc_dir:
            return SimpleNamespace(st_uid=0, st_gid=0, st_mode=info.st_mode)
        if path == config_dir:
            return SimpleNamespace(st_uid=0, st_gid=gid, st_mode=info.st_mode)
        if path == backup_parent:
            return SimpleNamespace(st_uid=0, st_gid=0, st_mode=info.st_mode)
        return info

    monkeypatch.setattr(Path, "stat", fake_stat)
    return config_dir, gid


def test_vps_service_unit_grants_only_dedicated_download_cache_write_path():
    import inspect
    assert "ReadWritePaths=/var/lib/r2-relay/cache /var/lib/r2-relay/downloads" in receiver.UNIT_TEXT
    assert "ReadWritePaths=/var/lib/r2-relay/cache" in receiver.PREVIOUS_UNIT_TEXT
    assert "PREVIOUS_UNIT_TEXT.strip()" in inspect.getsource(receiver.validate_host_state)


def test_download_cache_directory_is_created_private_for_relay_user(tmp_path, monkeypatch):
    import os
    import stat
    from types import SimpleNamespace

    parent = tmp_path / "var-lib-r2-relay"
    parent.mkdir(mode=0o755)
    target = parent / "downloads"
    monkeypatch.setattr(receiver, "DOWNLOAD_CACHE", target)
    monkeypatch.setattr(receiver.os, "chown", lambda *_args, **_kwargs: None)
    real_stat = Path.stat

    def fake_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if path == parent:
            return SimpleNamespace(st_uid=0, st_mode=info.st_mode)
        return info

    monkeypatch.setattr(Path, "stat", fake_stat)
    receiver._ensure_download_cache_directory(os.getuid(), os.getgid())

    info = target.stat()
    assert info.st_uid == os.getuid()
    assert info.st_gid == os.getgid()
    assert stat.S_IMODE(info.st_mode) == 0o700


def test_download_config_directory_is_created_root_owned_group_search_only(
    tmp_path, monkeypatch
):
    import stat

    config_dir, gid = _prepare_download_config_path_validation(tmp_path, monkeypatch)

    receiver._validate_download_config_paths(gid)

    info = config_dir.stat()
    assert info.st_uid == 0
    assert info.st_gid == gid
    assert stat.S_IMODE(info.st_mode) == 0o710


def test_download_config_directory_rejects_permissive_existing_mode(
    tmp_path, monkeypatch
):
    config_dir, gid = _prepare_download_config_path_validation(
        tmp_path, monkeypatch, existing_mode=0o755
    )

    try:
        receiver._validate_download_config_paths(gid)
    except receiver.DeployError as exc:
        assert str(exc) == "download_config_parent_permissions_invalid"
    else:
        raise AssertionError("permissive download config directory accepted")

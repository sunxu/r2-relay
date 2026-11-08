from pathlib import Path
import importlib.util
import io
import tarfile
import pytest

file=Path(__file__).resolve().parents[1]/"ops/r2_install_prepare.py"
spec=importlib.util.spec_from_file_location("prepare",file)
app=importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

def archive(entries):
    data=io.BytesIO()
    with tarfile.open(fileobj=data,mode="w") as tf:
        for name,kind in entries:
            info=tarfile.TarInfo(name)
            info.type=kind
            info.size=1 if kind==tarfile.REGTYPE else 0
            tf.addfile(info,io.BytesIO(b"x") if info.isfile() else None)
    return data.getvalue()

def test_reject_unsafe_tar():
    names=[(name,tarfile.REGTYPE) for name in app.REQUIRED]
    assert app.verify_members(archive(names))["files"]==len(names)
    for bad,kind in [("../etc/shadow",tarfile.REGTYPE),
                     ("/etc/passwd",tarfile.REGTYPE),
                     ("docs/link",tarfile.SYMTYPE),
                     ("docs/hardlink",tarfile.LNKTYPE)]:
        with pytest.raises(ValueError):
            app.verify_members(archive(names+[(bad,kind)]))

def test_dry_run_only(tmp_path,monkeypatch):
    dest=tmp_path/"app"
    monkeypatch.setattr(app.os,"geteuid",lambda:0)
    monkeypatch.setattr(app,"git_archive",lambda stage: archive(
        [(name,tarfile.REGTYPE) for name in app.REQUIRED]))
    result=app.prepare(dry_run=True,dest=dest)
    assert result["status"]=="DRY_RUN_PASS"
    assert not dest.exists()
    dest.mkdir()
    with pytest.raises(ValueError,match="destination_already_exists"):
        app.prepare(dry_run=True,dest=dest)

def test_no_shell_or_service_start():
    source=file.read_text()
    assert "shell=False" in source
    assert "systemctl" not in source
    assert "/opt/r2-relay" in source
    assert "user=\"r2relay\"" in source

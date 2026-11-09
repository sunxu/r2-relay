from pathlib import Path
import importlib.util

path = Path(__file__).resolve().parents[1] / "ops/generic_vps_mcp.py"
spec = importlib.util.spec_from_file_location("generic_vps_mcp", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def workspace(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir(mode=0o700)
    project = root / "r2-relay"
    project.mkdir()
    (project / "docs").mkdir()
    (project / "docs" / "hello.md").write_text("safe")
    (project / "README.md").write_text("readme")
    (project / ".env").write_text("secret")
    return root, project

def test_restrict_document_paths(tmp_path):
    root, project = workspace(tmp_path)
    assert mod.doc_read("r2-relay", "docs/hello.md", base=root)["content"] == "safe"
    assert mod.doc_read("r2-relay", "README.md", base=root)["content"] == "readme"
    for path in (".env", "../.env", "/etc/shadow", "docs/../.env",
                 "other/info.md", "docs/no-file.md", "docs/%2e%2e/file"):
        assert "error" in mod.doc_read("r2-relay", path, base=root)
    (project / "docs" / "linked.md").symlink_to(project / ".env")
    assert mod.doc_read("r2-relay", "docs/linked.md", base=root)["error"] == "symlink_denied"

def test_denies_symlink_project_and_base(tmp_path):
    root, project = workspace(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert "error" in mod.doc_read("r2-relay", "README.md", base=alias)
    project.rename(root / "moved")
    project.symlink_to(root / "moved", target_is_directory=True)
    assert "error" in mod.doc_read("r2-relay", "README.md", base=root)

def test_bad_git_stage_requests(tmp_path):
    root, _ = workspace(tmp_path)
    for sha in ("main", "a"*39, "A"*40, "a"*40+"x", "--help", ""):
        assert mod._stage("r2-relay", sha, base=root)["error"] == "full_sha_required"
    assert mod._stage("arbitrary", "a"*40, base=root)["error"] == "project_not_allowed"
    assert mod._stage("r2-relay", "a"*40, base=root)["error"] == "stage_already_exists"

def test_stage_fixed_argv(tmp_path, monkeypatch):
    root=tmp_path/"workspaces"
    root.mkdir(mode=0o700)
    calls=[]
    sha="a"*40
    def fake(args, **kwargs):
        calls.append(args)
        return {"exit_code":0,"stdout":sha+"\n" if "rev-parse" in args else "",
                "stderr":""}
    monkeypatch.setattr(mod, "run_fixed", fake)
    result=mod._stage("r2-relay", sha, base=root)
    assert result["status"] == "STAGED_NOT_DEPLOYED"
    assert len(calls) == 5
    assert all(x[0] == "/usr/bin/git" for x in calls)
    assert "https://github.com/sunxu/r2-relay.git" in calls[1]
    assert sha in calls[2]
    assert all("sudo" not in x for c in calls for x in c)

def test_snapshot_and_registration(monkeypatch):
    calls=[]
    def fake(args, **kwargs):
        calls.append(args)
        return {"exit_code":0,"stdout":"","stderr":""}
    monkeypatch.setattr(mod, "run_fixed", fake)
    snapshot=mod.system_snapshot()
    assert set(snapshot) == {"disk","memory","services","listeners"}
    assert all(x[0] in ("/usr/bin/df","/usr/bin/free",
                        "/usr/bin/systemctl","/usr/bin/ss") for x in calls)
    class App:
        def __init__(self): self.registered=[]
        def tool(self):
            def fn(f): self.registered.append(f.__name__); return f
            return fn
    app=App()
    mod.register(app)
    assert app.registered == ["vps_system_snapshot",
                             "vps_project_doc_read","vps_git_stage",
                             "vps_r2_install_prepare",
                             "vps_r2_runtime_smoke", "vps_r2_service_prepare",
                             "vps_r2_service_verify", "vps_r2_deployment_status",
                             "vps_r2_loopback_start", "vps_r2_https_preflight",
                             "vps_r2_https_prepare", "vps_r2_https_start",
                             "vps_r2_https_ca_fingerprint"]

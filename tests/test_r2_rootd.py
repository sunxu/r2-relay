"""Guard the root daemon against arbitrary requests and cross-UID calls."""
import importlib.util
from pathlib import Path

file = Path(__file__).resolve().parents[1] / "ops/r2_rootd.py"
spec = importlib.util.spec_from_file_location("r2_rootd", file)
daemon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daemon)

def test_peer_authentication(monkeypatch):
    monkeypatch.setattr(daemon, "execute", lambda x: (_ for _ in ()).throw(AssertionError()))
    assert daemon.process_request('{"action":"probe"}', 998, 999) == {"error": "unauthorized_peer"}

def test_invalid_schema_and_duplicates(monkeypatch):
    monkeypatch.setattr(daemon, "execute", lambda x: (_ for _ in ()).throw(AssertionError()))
    for payload in ('{"action":"root"}','{"action":"probe","cmd":"whoami"}',
                    '{"action":"probe","action":"relay_status"}',
                    '{"action":42}', '["probe"]', 'not-json'):
        assert "error" in daemon.process_request(payload, 999, 999)

def test_allowed_probe(monkeypatch):
    actions=[]
    monkeypatch.setattr(daemon, "execute", lambda x: actions.append(x) or {"ok": True})
    assert daemon.process_request('{"action":"probe"}', 999, 999) == {"ok": True}
    assert actions == ["probe"]

def test_fixed_command_argv(monkeypatch):
    commands=[]
    def capture(argv, timeout=180):
        commands.append(argv)
        return {"exit_code": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(daemon, "run", capture)
    for action in daemon.ACTIONS:
        daemon.execute(action)
    assert commands
    assert all(isinstance(c,list) and c[0].startswith("/") for c in commands)
    assert all("sing-box" not in " ".join(c) for c in commands)

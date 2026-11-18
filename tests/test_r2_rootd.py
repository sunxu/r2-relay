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
    def capture(argv, timeout=180, max_output=2500):
        commands.append(argv)
        return {"exit_code": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(daemon, "run", capture)
    for action in daemon.ACTIONS:
        daemon.execute(action)
    assert commands
    assert all(isinstance(c,list) and c[0].startswith("/") for c in commands)
    assert all("sing-box" not in " ".join(c) for c in commands)


def test_unified_deployment_status_uses_only_the_fixed_service_verifier(monkeypatch):
    calls = []
    verification = {
        "status": "SERVICE_VERIFY_PASS",
        "release_commit": "a" * 40,
        "env_exists": True,
        "service_state": "active",
        "token_emitted": False,
    }

    def fake(argv, timeout=180, max_output=2500):
        calls.append(argv)
        return {"exit_code": 0, "stdout": __import__("json").dumps(verification), "stderr": ""}

    monkeypatch.setattr(daemon, "run", fake)
    result = daemon.execute("r2_deployment_status")
    assert result["schema"] == "r2_relay_unified_deployment_status_v1"
    assert result["status"] == "UNIFIED_DEPLOYMENT_STATUS_PASS"
    assert result["release_matches"] is True
    assert result["environment_file_exists"] is True
    assert result["release_commit"] == "a" * 40
    assert result["modified"] is False and result["token_emitted"] is False
    assert result["real_r2_tested"] is False
    assert calls == [["/usr/bin/python3", "/usr/local/libexec/r2_loopback_control.py"]]


def test_unified_deployment_status_fails_closed_on_verification_failure(monkeypatch):
    verification = {
        "status": "LOOPBACK_CONTROL_FAILED",
        "reason": "environment_file_missing",
        "env_exists": False,
        "token_emitted": False,
    }

    monkeypatch.setattr(
        daemon, "run",
        lambda argv, timeout=180, max_output=2500: {
            "exit_code": 1,
            "stdout": __import__("json").dumps(verification),
            "stderr": "",
        },
    )
    result = daemon.execute("r2_deployment_status")
    assert result["status"] == "UNIFIED_DEPLOYMENT_STATUS_FAILED"
    assert result["release_matches"] is False
    assert result["environment_file_exists"] is False
    assert result["reason"] == "environment_file_missing"
    assert result["modified"] is False


def test_unified_deployment_status_fails_closed_on_invalid_output(monkeypatch):
    monkeypatch.setattr(
        daemon, "run",
        lambda argv, timeout=180, max_output=2500: {
            "exit_code": 0, "stdout": "not-json", "stderr": "",
        },
    )
    result = daemon.execute("r2_deployment_status")
    assert result["status"] == "UNIFIED_DEPLOYMENT_STATUS_FAILED"
    assert result["reason"] == "service_verification_output_invalid"
    assert result["modified"] is False

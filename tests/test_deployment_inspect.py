"""Guard against accidental arbitrary-command or secret inspection."""
import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "ops/deployment_inspect.py"
spec = importlib.util.spec_from_file_location("inspect", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_static_allowlists():
    assert mod.PORTS == (22, 443, 8443, 18085, 18765)
    assert not any("secret" in p for p in mod.FILES)


def test_inactive_is_not_the_same_as_not_installed(monkeypatch):
    def fake(args):
        if args[0].endswith("systemctl"):
            return 0, "LoadState=not-found\nActiveState=inactive\n"
        return 0, "LISTEN 0 128 127.0.0.1:18085 0.0.0.0:*\n"
    monkeypatch.setattr(mod, "_run", fake)
    result = mod.deployment_inspect()
    assert result["services"]["caddy.service"]["load"] == "not-found"
    assert result["services"]["caddy.service"]["active"] == "inactive"
    assert result["listening_ports"]["18085"] == ["127.0.0.1"]
    assert result["modified"] is False
    assert result["credentials_read"] is False


def test_no_path_argument_or_shell(monkeypatch):
    commands = []
    def fake(args):
        commands.append(args)
        return 0, ""
    monkeypatch.setattr(mod, "_run", fake)
    mod.deployment_inspect()
    assert len(commands) == len(mod.SERVICES) + 1
    assert all(cmd[0] in ("/usr/bin/ss", "/usr/bin/systemctl") for cmd in commands)
    assert all("Environment" not in " ".join(cmd) for cmd in commands)

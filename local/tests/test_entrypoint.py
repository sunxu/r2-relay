from pathlib import Path

from r2relay_local.__main__ import command


def test_python_entrypoint_preserves_local_port_and_target_mitm(monkeypatch):
    monkeypatch.setattr("r2relay_local.__main__.shutil.which", lambda _: "/fake/mitmdump")
    args = command("127.0.0.1", 18585, Path("/tmp/addon_entry.py"))
    assert args[args.index("--listen-port") + 1] == "18585"
    assert args[args.index("--listen-host") + 1] == "127.0.0.1"
    assert "r2" in args[args.index("--allow-hosts") + 1]
    assert args[-1] == "/tmp/addon_entry.py"

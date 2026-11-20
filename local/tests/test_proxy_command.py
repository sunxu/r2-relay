import re
from pathlib import Path

from r2relay_local import __main__ as entry


def test_mitmproxy_allow_hosts_includes_r2_and_loopback_response_server(monkeypatch):
    monkeypatch.setattr(entry.shutil, "which", lambda name: "/usr/bin/mitmdump")
    args = entry.command("127.0.0.1", 18585, Path("/tmp/addon_entry.py"))
    pattern = args[args.index("--allow-hosts") + 1]

    assert re.fullmatch(pattern, "bucket.account.r2.cloudflarestorage.com:443")
    assert re.fullmatch(pattern, "127.0.0.1:49123")
    assert not re.fullmatch(pattern, "example.com:443")

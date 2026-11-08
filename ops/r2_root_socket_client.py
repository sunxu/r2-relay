"""Unprivileged MCP adapter: no sudo or arbitrary command execution."""
import json
import socket
from typing import Literal

SOCKET = "/run/r2-rootd/control.sock"
ALLOWED = frozenset(("probe", "install_prereqs", "prepare_dirs", "relay_status"))

def root_action_via_socket(
    action: Literal["probe", "install_prereqs", "prepare_dirs", "relay_status"]
) -> dict:
    if action not in ALLOWED:
        return {"error": "action_not_allowed"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(330)
            client.connect(SOCKET)
            client.sendall(json.dumps({"action": action}).encode())
            chunks = []
            size = 0
            while True:
                buf = client.recv(4096)
                if not buf:
                    break
                size += len(buf)
                if size > 32768:
                    return {"error": "response_too_large"}
                chunks.append(buf)
            return json.loads(b"".join(chunks))
    except (OSError, ValueError, UnicodeError):
        return {"error": "rootd_unavailable"}

import importlib.util
from pathlib import Path
import pytest

path = Path(__file__).resolve().parents[1] / "ops/bootstrap_generic_vps_mcp.py"
spec = importlib.util.spec_from_file_location("bootstrap_generic", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def test_inject_once_and_preserve_existing_tools():
    source = (
        "from mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('service', host='127.0.0.1')\n"
        "@mcp.tool()\ndef host_info():\n    return {'ok': True}\n"
        "if __name__ == '__main__':\n    mcp.run()\n"
    )
    updated = mod.add_registration(source)
    assert updated.count("register_generic_vps(mcp)") == 1
    assert updated.index("register_generic_vps(mcp)") < updated.index("@mcp.tool()")
    assert "def host_info()" in updated
    assert mod.add_registration(updated) == updated
    compile(updated, "test", "exec")

def test_multiline_fastmcp_assignment():
    source = "mcp = FastMCP(\n    'x',\n    port=18765,\n)\n"
    assert "register_generic_vps(mcp)" in mod.add_registration(source)

def test_fail_closed_on_ambiguous_or_missing():
    for source in ("x = 2\n", "mcp = other()\n",
                   "mcp = FastMCP('a')\nmcp = FastMCP('b')\n"):
        with pytest.raises(ValueError):
            mod.add_registration(source)

def test_no_shell_or_unsafe_install_operations():
    body = path.read_text(encoding="utf-8")
    assert "shell=False" in body
    assert "sing-box" not in body.replace("Does not alter sing-box/443.", "")
    assert "NoNewPrivileges=no" not in body

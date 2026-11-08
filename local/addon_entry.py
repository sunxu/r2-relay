"""Mitmdump script entrypoint. Import the installed local package."""
import os

from r2relay_local.addon import R2RelayAddon, RelayClient

endpoint = os.environ.get("R2_RELAY_VPS_URL", "https://127.0.0.1:8443")
token = os.environ.get("R2_RELAY_TOKEN", "")
if not token:
    raise RuntimeError("R2_RELAY_TOKEN is required")

addons = [R2RelayAddon(RelayClient(endpoint, token))]

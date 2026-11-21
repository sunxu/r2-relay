"""Mitmdump script entrypoint. Import the installed local package."""
from r2relay_local.addon import build_addon

addons = [build_addon()]

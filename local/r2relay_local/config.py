"""Validated TOML configuration for the local R2 upload cache buffer."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import tomllib

_MIB = 1024 * 1024
DEFAULT_CONFIG = Path(__file__).with_name("defaults.toml")
LOCAL_CONFIG = Path.home() / ".config" / "r2-relay" / "config.toml"


@dataclass(frozen=True)
class Settings:
    max_part_bytes: int
    global_budget_bytes: int
    source: Path

    @property
    def max_part_mib(self) -> int:
        return self.max_part_bytes // _MIB

    @property
    def global_buffer_mib(self) -> int:
        return self.global_budget_bytes // _MIB


def _merge(base: dict, override: dict, prefix: str = "") -> None:
    for key, value in override.items():
        name = prefix + key
        if key not in base:
            raise ValueError(f"unknown config key: {name}")
        original = base[key]
        if isinstance(original, dict):
            if not isinstance(value, dict):
                raise ValueError(f"{name} must be a section")
            _merge(original, value, name + ".")
        elif type(original) is not type(value):
            raise ValueError(f"wrong type for {name}")
        else:
            base[key] = value


def load_settings(config_path: Path | None = None) -> Settings:
    """Load packaged defaults plus an optional user-owned TOML override."""
    with DEFAULT_CONFIG.open("rb") as stream:
        data = tomllib.load(stream)
    local = config_path or Path(
        os.environ.get("R2_RELAY_CONFIG", str(LOCAL_CONFIG))
    ).expanduser()
    source = DEFAULT_CONFIG
    if local.is_file():
        with local.open("rb") as stream:
            _merge(data, tomllib.load(stream))
        source = local
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported R2 Relay config schema_version")
    upload = data["upload"]
    max_part_mib = upload["max_part_mib"]
    global_buffer_mib = upload["global_buffer_mib"]
    if type(max_part_mib) is not int or not 1 <= max_part_mib <= 4096:
        raise ValueError("upload.max_part_mib must be an integer from 1 to 4096")
    if type(global_buffer_mib) is not int or not 1 <= global_buffer_mib <= 16384:
        raise ValueError("upload.global_buffer_mib must be an integer from 1 to 16384")
    if max_part_mib > global_buffer_mib:
        raise ValueError("upload.max_part_mib cannot exceed upload.global_buffer_mib")
    return Settings(
        max_part_bytes=max_part_mib * _MIB,
        global_budget_bytes=global_buffer_mib * _MIB,
        source=source,
    )

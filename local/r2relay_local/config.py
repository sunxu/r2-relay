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
    download_enabled: bool = False
    download_backend: str = "remote"
    download_local_chunk_bytes: int = 2 * _MIB
    download_local_min_bytes: int = 2 * _MIB
    download_local_max_connections_per_get: int = 8
    download_local_global_max_connections: int = 128
    download_max_concurrency: int = 16
    download_global_max_concurrency: int = 32
    download_small_memory_bytes: int = 0
    download_max_temp_bytes: int = 0
    download_temp_dir: Path = Path.home() / ".cache" / "r2-relay" / "downloads"

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

    download = data["download"]
    enabled = download["enabled"]
    backend = download["backend"]
    if type(backend) is not str or backend not in ("remote", "local"):
        raise ValueError("download.backend must be remote or local")
    local_download = download["local"]
    for name, value, minimum, maximum in (
        ("download.local.chunk_size_mib", local_download["chunk_size_mib"], 2, 2),
        ("download.local.min_size_mib", local_download["min_size_mib"], 2, 2),
        ("download.local.max_connections_per_get", local_download["max_connections_per_get"], 1, 8),
        ("download.local.global_max_connections", local_download["global_max_connections"], 1, 128),
    ):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    if local_download["max_connections_per_get"] > local_download["global_max_connections"]:
        raise ValueError("download.local.max_connections_per_get exceeds global_max_connections")
    if type(enabled) is not bool:
        raise ValueError("download.enabled must be a boolean")
    max_concurrency = download["max_concurrency"]
    global_max_concurrency = download["global_max_concurrency"]
    small_memory_mib = download["small_response_memory_mib"]
    max_temp_mib = download["max_temp_mib"]
    temp_dir = download["temp_dir"]
    for name, value, minimum, maximum in (
        ("download.max_concurrency", max_concurrency, 1, 128),
        ("download.global_max_concurrency", global_max_concurrency, 1, 256),
        ("download.small_response_memory_mib", small_memory_mib, 0, 8192),
        ("download.max_temp_mib", max_temp_mib, 0, 1048576),
    ):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    if max_concurrency > global_max_concurrency:
        raise ValueError("download.max_concurrency cannot exceed download.global_max_concurrency")
    if not isinstance(temp_dir, str) or not temp_dir.strip():
        raise ValueError("download.temp_dir must be a non-empty path")
    if enabled and backend == "remote":
        if small_memory_mib < 24:
            raise ValueError("download.small_response_memory_mib must be at least 24 when enabled")
        if max_temp_mib < 25:
            raise ValueError("download.max_temp_mib must be at least 25 when enabled")
    if enabled and backend == "local" and max_temp_mib < 2:
        raise ValueError("download.max_temp_mib must be at least 2 when local enabled")

    return Settings(
        max_part_bytes=max_part_mib * _MIB,
        global_budget_bytes=global_buffer_mib * _MIB,
        source=source,
        download_enabled=enabled,
        download_backend=backend,
        download_local_chunk_bytes=local_download["chunk_size_mib"] * _MIB,
        download_local_min_bytes=local_download["min_size_mib"] * _MIB,
        download_local_max_connections_per_get=local_download["max_connections_per_get"],
        download_local_global_max_connections=local_download["global_max_connections"],
        download_max_concurrency=max_concurrency,
        download_global_max_concurrency=global_max_concurrency,
        download_small_memory_bytes=small_memory_mib * _MIB,
        download_max_temp_bytes=max_temp_mib * _MIB,
        download_temp_dir=Path(temp_dir).expanduser(),
    )

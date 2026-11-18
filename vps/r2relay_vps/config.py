"""R2 Relay VPS TOML configuration, including the isolated download service."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

DEFAULT_CONFIG = Path(__file__).with_name("defaults.toml")
SYSTEM_CONFIG = Path("/etc/r2-relay/config.toml")
_MIB = 1024 * 1024


@dataclass(frozen=True)
class DownloadSettings:
    enabled: bool
    part_size_bytes: int
    split_threshold_bytes: int
    cache_ttl_seconds: int
    max_cache_bytes: int
    min_free_bytes: int
    max_active_tasks: int
    max_waiters: int
    wait_timeout_seconds: int


def load_download_settings(config_path: Path | None = None) -> DownloadSettings:
    """Load packaged defaults and the optional system TOML override.

    The download service is disabled until an operator supplies explicit disk
    and task budgets in /etc/r2-relay/config.toml. The four agreed transfer
    values are TOML settings and are never read from environment variables.
    """
    with DEFAULT_CONFIG.open("rb") as stream:
        data = tomllib.load(stream)
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported VPS Relay config schema_version")
    path = config_path or SYSTEM_CONFIG
    if path.is_file():
        with path.open("rb") as stream:
            override = tomllib.load(stream)
        section = override.get("download", {})
        if not isinstance(section, dict):
            raise ValueError("[download] must be a TOML table")
        unknown = set(section) - set(data["download"])
        if unknown:
            raise ValueError(f"unknown [download] setting(s): {', '.join(sorted(unknown))}")
        data["download"].update(section)

    section = data["download"]
    enabled = section["enabled"]
    if type(enabled) is not bool:
        raise ValueError("download.enabled must be a boolean")

    integer_ranges = {
        "part_size_mib": (1, 1024),
        "split_threshold_mib": (1, 4096),
        "cache_ttl_seconds": (1, 86400),
        "max_cache_bytes": (0, 2**63 - 1),
        "min_free_bytes": (0, 2**63 - 1),
        "max_active_tasks": (0, 4096),
        "max_waiters": (0, 65536),
        "wait_timeout_seconds": (1, 300),
    }
    for key, (minimum, maximum) in integer_ranges.items():
        value = section[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"download.{key} must be an integer from {minimum} to {maximum}")

    part_size = section["part_size_mib"]
    threshold = section["split_threshold_mib"]
    if threshold < part_size:
        raise ValueError("download.split_threshold_mib cannot be smaller than part_size_mib")
    if enabled:
        for key in ("max_cache_bytes", "min_free_bytes", "max_active_tasks", "max_waiters"):
            if section[key] <= 0:
                raise ValueError(f"download.{key} must be positive when download.enabled=true")

    return DownloadSettings(
        enabled=enabled,
        part_size_bytes=part_size * _MIB,
        split_threshold_bytes=threshold * _MIB,
        cache_ttl_seconds=section["cache_ttl_seconds"],
        max_cache_bytes=section["max_cache_bytes"],
        min_free_bytes=section["min_free_bytes"],
        max_active_tasks=section["max_active_tasks"],
        max_waiters=section["max_waiters"],
        wait_timeout_seconds=section["wait_timeout_seconds"],
    )

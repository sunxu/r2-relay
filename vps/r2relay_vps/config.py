"""R2 Relay VPS TOML configuration, including the isolated download service."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
import tomllib

DEFAULT_CONFIG = Path(__file__).with_name("defaults.toml")
DOWNLOAD_CONFIG = Path("/etc/r2-relay-download/download.toml")
DOWNLOAD_CONFIG_SCHEMA = "r2-relay-download-config-dropin-v1"
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
    max_retained_tasks: int
    max_waiters: int
    wait_timeout_seconds: int
    test_expires_at_epoch: int = 0


def load_download_settings(
    config_path: Path | None = None,
    download_config_path: Path | None = None,
) -> DownloadSettings:
    """Load package defaults, an explicitly supplied general config, then the managed drop-in.

    Production startup intentionally does not probe /etc/r2-relay/config.toml:
    that root-only directory also contains the private EnvironmentFile.
    The controlled download drop-in lives in /etc/r2-relay-download/download.toml,
    whose directory grants the service account search-only access.
    """
    with DEFAULT_CONFIG.open("rb") as stream:
        data = tomllib.load(stream)
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported VPS Relay config schema_version")

    def merge_download_override(path: Path, *, strict: bool) -> None:
        if not path.is_file():
            return
        with path.open("rb") as stream:
            override = tomllib.load(stream)
        if "schema_version" in override and (
            type(override["schema_version"]) is not int or override["schema_version"] != 1
        ):
            raise ValueError("unsupported system config schema_version")
        if strict and set(override) - {"schema_version", "download"}:
            raise ValueError("download config drop-in contains unknown top-level fields")
        section = override.get("download", {})
        if not isinstance(section, dict):
            raise ValueError("[download] must be a TOML table")
        unknown = set(section) - set(data["download"])
        if unknown:
            raise ValueError(f"unknown [download] setting(s): {', '.join(sorted(unknown))}")
        data["download"].update(section)

    if config_path is not None:
        merge_download_override(config_path, strict=False)
    merge_download_override(download_config_path or DOWNLOAD_CONFIG, strict=True)

    section = data["download"]
    enabled = section["enabled"]
    if type(enabled) is not bool:
        raise ValueError("download.enabled must be a boolean")

    integer_minimums = {
        "part_size_mib": 1,
        "split_threshold_mib": 1,
        "cache_ttl_seconds": 1,
        "max_cache_bytes": 0,
        "min_free_bytes": 0,
        "max_active_tasks": 0,
        "max_retained_tasks": 0,
        "max_waiters": 0,
        "wait_timeout_seconds": 1,
        "test_expires_at_epoch": 0,
    }
    for key, minimum in integer_minimums.items():
        value = section[key]
        if type(value) is not int or value < minimum:
            raise ValueError(f"download.{key} must be an integer >= {minimum}")
    # Keep signed 64-bit bounds for byte counters and epoch interoperability.
    for key in ("max_cache_bytes", "min_free_bytes", "test_expires_at_epoch"):
        if section[key] > 2**63 - 1:
            raise ValueError(f"download.{key} must fit in a signed 64-bit integer")

    part_size = section["part_size_mib"]
    threshold = section["split_threshold_mib"]
    if threshold < part_size:
        raise ValueError("download.split_threshold_mib cannot be smaller than part_size_mib")
    if enabled:
        for key in ("max_cache_bytes", "min_free_bytes", "max_active_tasks", "max_retained_tasks", "max_waiters"):
            if section[key] <= 0:
                raise ValueError(f"download.{key} must be positive when download.enabled=true")

    test_expires_at_epoch = section["test_expires_at_epoch"]
    if test_expires_at_epoch and int(time.time()) >= test_expires_at_epoch:
        enabled = False

    return DownloadSettings(
        enabled=enabled,
        part_size_bytes=part_size * _MIB,
        split_threshold_bytes=threshold * _MIB,
        cache_ttl_seconds=section["cache_ttl_seconds"],
        max_cache_bytes=section["max_cache_bytes"],
        min_free_bytes=section["min_free_bytes"],
        max_active_tasks=section["max_active_tasks"],
        max_retained_tasks=section["max_retained_tasks"],
        max_waiters=section["max_waiters"],
        wait_timeout_seconds=section["wait_timeout_seconds"],
        test_expires_at_epoch=test_expires_at_epoch,
    )

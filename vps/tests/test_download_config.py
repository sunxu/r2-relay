from pathlib import Path

import pytest

from r2relay_vps.config import load_download_settings


def test_vps_download_defaults_are_safe_and_use_owner_values():
    settings = load_download_settings(Path("/path/that/does/not/exist.toml"))
    assert settings.enabled is False
    assert settings.part_size_bytes == 16 * 1024 * 1024
    assert settings.split_threshold_bytes == 24 * 1024 * 1024
    assert settings.cache_ttl_seconds == 600


def test_vps_download_settings_are_loaded_from_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[download]\n"
        "enabled = true\n"
        "part_size_mib = 16\n"
        "split_threshold_mib = 24\n"
        "cache_ttl_seconds = 600\n"
        "max_cache_bytes = 1073741824\n"
        "min_free_bytes = 268435456\n"
        "max_active_tasks = 8\n"
        "max_waiters = 64\n"
        "wait_timeout_seconds = 30\n",
        encoding="utf-8",
    )
    settings = load_download_settings(path)
    assert settings.enabled is True
    assert settings.part_size_bytes == 16 * 1024 * 1024
    assert settings.split_threshold_bytes == 24 * 1024 * 1024
    assert settings.max_cache_bytes == 1024 * 1024 * 1024
    assert settings.max_active_tasks == 8


@pytest.mark.parametrize(
    "value",
    [
        "part_size_mib = 16.0",
        "split_threshold_mib = 8",
        "cache_ttl_seconds = 0",
        "max_cache_bytes = -1",
    ],
)
def test_vps_download_invalid_toml_fails_closed(tmp_path, value):
    path = tmp_path / "bad.toml"
    path.write_text(
        "[download]\n" + value + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_download_settings(path)


def test_enabling_download_without_disk_budgets_fails_closed(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[download]\nenabled = true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="max_cache_bytes"):
        load_download_settings(path)

from pathlib import Path

import httpx
import pytest

import r2relay_vps.config as config
from r2relay_vps.app import create_app
from r2relay_vps.cache import CacheStore
from r2relay_vps.config import load_download_settings


def test_vps_download_defaults_are_safe_and_use_owner_values():
    settings = load_download_settings(Path("/path/that/does/not/exist.toml"))
    assert settings.enabled is False
    assert settings.part_size_bytes == 16 * 1024 * 1024
    assert settings.split_threshold_bytes == 24 * 1024 * 1024
    assert settings.cache_ttl_seconds == 600
    assert settings.test_expires_at_epoch == 0


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
        "max_retained_tasks = 32\n"
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
    assert settings.max_retained_tasks == 32


def test_expired_real_r2_test_profile_fails_closed_at_service_start(tmp_path, monkeypatch):
    import time

    path = tmp_path / "expired-test.toml"
    path.write_text(
        "[download]\n"
        "enabled = true\n"
        "part_size_mib = 16\n"
        "split_threshold_mib = 24\n"
        "cache_ttl_seconds = 600\n"
        "max_cache_bytes = 67108864\n"
        "min_free_bytes = 5368709120\n"
        "max_active_tasks = 1\n"
        "max_retained_tasks = 2\n"
        "max_waiters = 4\n"
        "wait_timeout_seconds = 30\n"
        "test_expires_at_epoch = 1\n",
        encoding="utf-8",
    )

    settings = load_download_settings(path)

    assert settings.enabled is False
    assert settings.test_expires_at_epoch == 1


def test_future_real_r2_test_profile_remains_enabled_before_expiry(tmp_path):
    import time

    path = tmp_path / "future-test.toml"
    path.write_text(
        "[download]\n"
        "enabled = true\n"
        "part_size_mib = 16\n"
        "split_threshold_mib = 24\n"
        "cache_ttl_seconds = 600\n"
        "max_cache_bytes = 67108864\n"
        "min_free_bytes = 5368709120\n"
        "max_active_tasks = 1\n"
        "max_retained_tasks = 2\n"
        "max_waiters = 4\n"
        "wait_timeout_seconds = 30\n"
        f"test_expires_at_epoch = {int(time.time()) + 600}\n",
        encoding="utf-8",
    )

    settings = load_download_settings(path)

    assert settings.enabled is True


@pytest.mark.parametrize(
    "value",
    [
        "part_size_mib = 16.0",
        "split_threshold_mib = 8",
        "cache_ttl_seconds = 0",
        "max_cache_bytes = -1",
        "test_expires_at_epoch = -1",
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



def test_managed_download_dropin_overrides_general_config_without_touching_it(tmp_path):
    general = tmp_path / "config.toml"
    general.write_text(
        "[download]\n"
        "enabled = true\n"
        "part_size_mib = 16\n"
        "split_threshold_mib = 24\n"
        "cache_ttl_seconds = 600\n"
        "max_cache_bytes = 536870912\n"
        "min_free_bytes = 4294967296\n"
        "max_active_tasks = 2\n"
        "max_retained_tasks = 16\n"
        "max_waiters = 32\n"
        "wait_timeout_seconds = 30\n",
        encoding="utf-8",
    )
    original_general = general.read_bytes()
    dropin = tmp_path / "download.toml"
    dropin.write_text(
        "schema_version = 1\n"
        "[download]\n"
        "enabled = false\n"
        "part_size_mib = 16\n"
        "split_threshold_mib = 24\n"
        "cache_ttl_seconds = 600\n"
        "max_cache_bytes = 0\n"
        "min_free_bytes = 0\n"
        "max_active_tasks = 0\n"
        "max_retained_tasks = 0\n"
        "max_waiters = 0\n"
        "wait_timeout_seconds = 30\n",
        encoding="utf-8",
    )

    settings = load_download_settings(general, dropin)

    assert settings.enabled is False
    assert settings.part_size_bytes == 16 * 1024 * 1024
    assert settings.split_threshold_bytes == 24 * 1024 * 1024
    assert settings.cache_ttl_seconds == 600
    assert settings.max_cache_bytes == 0
    assert general.read_bytes() == original_general


def test_managed_download_dropin_rejects_unknown_top_level_fields(tmp_path):
    dropin = tmp_path / "download.toml"
    dropin.write_text(
        "schema_version = 1\n"
        "unrelated = true\n"
        "[download]\n"
        "enabled = false\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown top-level"):
        load_download_settings(
            Path("/path/that/does/not/exist.toml"), dropin
        )


def test_managed_download_dropin_rejects_invalid_values(tmp_path):
    dropin = tmp_path / "download.toml"
    dropin.write_text(
        "[download]\n"
        "enabled = false\n"
        "part_size_mib = 16\n"
        "split_threshold_mib = 8\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot be smaller"):
        load_download_settings(
            Path("/path/that/does/not/exist.toml"), dropin
        )



def _write_disabled_dropin(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema_version = 1\n"
        "[download]\n"
        "enabled = false\n"
        "part_size_mib = 16\n"
        "split_threshold_mib = 24\n"
        "cache_ttl_seconds = 600\n"
        "max_cache_bytes = 0\n"
        "min_free_bytes = 0\n"
        "max_active_tasks = 0\n"
        "max_retained_tasks = 0\n"
        "max_waiters = 0\n"
        "wait_timeout_seconds = 30\n",
        encoding="utf-8",
    )


def _deny_legacy_general_config_probe(monkeypatch):
    original_is_file = Path.is_file
    legacy_path = Path("/etc/r2-relay/config.toml")

    def guarded_is_file(path):
        if path == legacy_path:
            raise PermissionError("legacy general config directory is root-only")
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", guarded_is_file)


def test_default_loader_uses_isolated_dropin_without_probing_root_only_config(
    tmp_path, monkeypatch
):
    dropin = tmp_path / "etc-r2-relay-download" / "download.toml"
    _write_disabled_dropin(dropin)
    monkeypatch.setattr(config, "DOWNLOAD_CONFIG", dropin)
    _deny_legacy_general_config_probe(monkeypatch)

    settings = config.load_download_settings()

    assert settings.enabled is False
    assert settings.part_size_bytes == 16 * 1024 * 1024
    assert settings.split_threshold_bytes == 24 * 1024 * 1024


@pytest.mark.asyncio
async def test_create_app_healthz_fails_closed_for_expired_test_dropin(tmp_path, monkeypatch):
    dropin = tmp_path / "etc-r2-relay-download" / "download.toml"
    dropin.parent.mkdir(parents=True)
    dropin.write_text(
        "schema_version = 1\\n"
        "[download]\\n"
        "enabled = true\\n"
        "part_size_mib = 16\\n"
        "split_threshold_mib = 24\\n"
        "cache_ttl_seconds = 600\\n"
        "max_cache_bytes = 67108864\\n"
        "min_free_bytes = 5368709120\\n"
        "max_active_tasks = 1\\n"
        "max_retained_tasks = 2\\n"
        "max_waiters = 4\\n"
        "wait_timeout_seconds = 30\\n"
        "test_expires_at_epoch = 1\\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "DOWNLOAD_CONFIG", dropin)
    app = create_app(
        CacheStore(tmp_path / "cache"),
        token="test-token",
        download_root=tmp_path / "downloads",
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "download_enabled": False}
    finally:
        await app.state.downloads.close()


@pytest.mark.asyncio
async def test_create_app_healthz_with_disabled_dropin_and_unreadable_general_config(
    tmp_path, monkeypatch
):
    dropin = tmp_path / "etc-r2-relay-download" / "download.toml"
    _write_disabled_dropin(dropin)
    monkeypatch.setattr(config, "DOWNLOAD_CONFIG", dropin)
    _deny_legacy_general_config_probe(monkeypatch)

    app = create_app(
        CacheStore(tmp_path / "cache"),
        token="test-token",
        download_root=tmp_path / "downloads",
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "download_enabled": False,
        }
    finally:
        await app.state.downloads.close()

import tempfile
import unittest
from pathlib import Path

from r2relay_local.config import load_settings


class UploadBufferConfigTests(unittest.TestCase):
    def test_packaged_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp) / "missing.toml")
        self.assertEqual(settings.max_part_mib, 128)
        self.assertEqual(settings.global_buffer_mib, 1024)
        self.assertFalse(settings.download_enabled)
        self.assertEqual(settings.download_backend, "remote")
        self.assertEqual(settings.download_local_chunk_bytes, 2 * 1024 * 1024)
        self.assertEqual(settings.download_local_global_max_connections, 128)
        self.assertEqual(settings.download_max_concurrency, 16)
        self.assertEqual(settings.download_global_max_concurrency, 32)
        self.assertEqual(settings.download_small_memory_bytes, 0)
        self.assertEqual(settings.download_max_temp_bytes, 0)
        self.assertFalse(settings.download_predownload_enabled)
        self.assertEqual(settings.download_predownload_dir, Path.home() / ".cache" / "r2-relay" / "predownload")

    def test_local_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "schema_version = 1\n[upload]\n"
                "max_part_mib = 64\n"
                "global_buffer_mib = 256\n"
                "[download]\n"
                "enabled = true\n"
                "max_temp_mib = 4096\n"
                'temp_dir = "~/r2-download-test"\n'
                "[download.remote]\n"
                "max_concurrency = 16\n"
                "global_max_concurrency = 32\n"
                "small_response_memory_mib = 64\n",
                encoding="utf-8",
            )
            settings = load_settings(path)
        self.assertEqual(settings.max_part_mib, 64)
        self.assertEqual(settings.global_buffer_mib, 256)
        self.assertTrue(settings.download_enabled)
        self.assertEqual(settings.download_small_memory_bytes, 64 * 1024 * 1024)
        self.assertEqual(settings.download_max_temp_bytes, 4096 * 1024 * 1024)
        self.assertEqual(settings.download_temp_dir, Path.home() / "r2-download-test")

    def test_local_does_not_require_remote_small_memory_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '[download]\nenabled = true\nbackend = "local"\n'
                'max_temp_mib = 64\n'
                '[download.local]\nmax_connections_per_get = 8\n'
                'global_max_connections = 128\n',
                encoding="utf-8",
            )
            settings = load_settings(path)
            self.assertEqual(settings.download_backend, "local")
            self.assertEqual(settings.download_small_memory_bytes, 0)

    def test_can_configure_resource_limits_above_previous_ceilings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[upload]\n"
                "max_part_mib = 4097\n"
                "global_buffer_mib = 16385\n"
                "[download]\n"
                "enabled = true\n"
                'backend = "local"\n'
                "max_temp_mib = 1048577\n"
                "[download.remote]\n"
                "max_concurrency = 129\n"
                "global_max_concurrency = 257\n"
                "small_response_memory_mib = 8193\n"
                "[download.local]\n"
                "max_connections_per_get = 9\n"
                "global_max_connections = 129\n",
                encoding="utf-8",
            )
            loaded = load_settings(path)
        self.assertEqual(loaded.max_part_mib, 4097)
        self.assertEqual(loaded.global_buffer_mib, 16385)
        self.assertEqual(loaded.download_max_concurrency, 129)
        self.assertEqual(loaded.download_global_max_concurrency, 257)
        self.assertEqual(loaded.download_small_memory_bytes, 8193 * 1024 * 1024)
        self.assertEqual(loaded.download_max_temp_bytes, 1048577 * 1024 * 1024)
        self.assertEqual(loaded.download_local_max_connections_per_get, 9)
        self.assertEqual(loaded.download_local_global_max_connections, 129)

    def test_configurable_local_chunk_and_minimum_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for chunk_mib, min_mib in ((1, 1), (4, 1), (1, 6), (8, 8)):
                with self.subTest(chunk_mib=chunk_mib, min_mib=min_mib):
                    path.write_text(
                        f"[download.local]\nchunk_size_mib = {chunk_mib}\n"
                        f"min_size_mib = {min_mib}\n",
                        encoding="utf-8",
                    )
                    loaded = load_settings(path)
                    self.assertEqual(loaded.download_local_chunk_bytes, chunk_mib * 1024 * 1024)
                    self.assertEqual(loaded.download_local_min_bytes, min_mib * 1024 * 1024)

    def test_predownload_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[download.predownload]\nenabled = true\n", encoding="utf-8")
            self.assertTrue(load_settings(path).download_predownload_enabled)

    def test_remote_settings_are_read_only_from_remote_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[download.remote]\nmax_concurrency = 6\n"
                "global_max_concurrency = 12\n"
                "small_response_memory_mib = 32\n",
                encoding="utf-8",
            )
            loaded = load_settings(path)
            self.assertEqual(loaded.download_max_concurrency, 6)
            self.assertEqual(loaded.download_global_max_concurrency, 12)
            self.assertEqual(loaded.download_small_memory_bytes, 32 * 1024 * 1024)

    def test_legacy_remote_keys_fail_with_unknown_config_key(self):
        for name, value in (
            ("max_concurrency", 16),
            ("global_max_concurrency", 32),
            ("small_response_memory_mib", 24),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.toml"
                path.write_text(
                    f'[download]\nbackend = "local"\n{name} = {value}\n',
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError) as raised:
                    load_settings(path)
                self.assertEqual(
                    str(raised.exception), f"unknown config key: download.{name}"
                )

    def test_rejects_invalid_or_unknown_values(self):
        invalid = (
            "schema_version = 1\n[upload]\nmax_part_mib = 0\n",
            "schema_version = 1\n[upload]\nglobal_buffer_mib = 0\n",
            "schema_version = 1\n[upload]\nmax_part_mib = 2048\nglobal_buffer_mib = 1024\n",
            "schema_version = 1\n[upload]\nunknown = 1\n",
            "schema_version = 2\n",
            "schema_version = 1\n[download]\nenabled = true\nmax_temp_mib = 128\n[download.remote]\nsmall_response_memory_mib = 8\n",
            "schema_version = 1\n[download]\nenabled = true\nmax_temp_mib = 16\n[download.remote]\nsmall_response_memory_mib = 24\n",
            "schema_version = 1\n[download.remote]\nmax_concurrency = 33\nglobal_max_concurrency = 32\n",
            'schema_version = 1\n[download]\nbackend = "auto"\n',
            # Old remote-only keys must be rejected even when values match defaults.
            'schema_version = 1\n[download]\nmax_concurrency = 16\n',
            'schema_version = 1\n[download]\nglobal_max_concurrency = 32\n',
            'schema_version = 1\n[download]\nsmall_response_memory_mib = 24\n',
            'schema_version = 1\n[download.predownload]\nenabled = 1\n',
            'schema_version = 1\n[download.predownload]\nunknown = true\n',
            'schema_version = 1\n[download.local]\nchunk_size_mib = 0\n',
            'schema_version = 1\n[download.local]\nchunk_size_mib = -1\n',
            'schema_version = 1\n[download.local]\nchunk_size_mib = 1.5\n',
            'schema_version = 1\n[download.local]\nmin_size_mib = 0\n',
            'schema_version = 1\n[download.local]\nmin_size_mib = -1\n',
            'schema_version = 1\n[download.local]\nmin_size_mib = 1.5\n',
            'schema_version = 1\n[download.local]\nmax_connections_per_get = 8\nglobal_max_connections = 4\n',
            'schema_version = 1\n[download.local]\nmax_connections_per_get = 0\n',
            'schema_version = 1\n[download.local]\nglobal_max_connections = 0\n',
            'schema_version = 1\n[download.remote]\nmax_concurrency = 0\n',
            'schema_version = 1\n[download.remote]\nsmall_response_memory_mib = -1\n',
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for content in invalid:
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(ValueError):
                    load_settings(path)


if __name__ == "__main__":
    unittest.main()

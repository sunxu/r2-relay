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

    def test_local_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "schema_version = 1\n[upload]\n"
                "max_part_mib = 64\n"
                "global_buffer_mib = 256\n"
                "[download]\n"
                "enabled = true\n"
                "max_concurrency = 16\n"
                "global_max_concurrency = 32\n"
                "small_response_memory_mib = 64\n"
                "max_temp_mib = 4096\n"
                'temp_dir = "~/r2-download-test"\n',
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

    def test_rejects_invalid_or_unknown_values(self):
        invalid = (
            "schema_version = 1\n[upload]\nmax_part_mib = 0\n",
            "schema_version = 1\n[upload]\nglobal_buffer_mib = 0\n",
            "schema_version = 1\n[upload]\nmax_part_mib = 2048\nglobal_buffer_mib = 1024\n",
            "schema_version = 1\n[upload]\nunknown = 1\n",
            "schema_version = 2\n",
            "schema_version = 1\n[download]\nenabled = true\nsmall_response_memory_mib = 8\nmax_temp_mib = 128\n",
            "schema_version = 1\n[download]\nenabled = true\nsmall_response_memory_mib = 24\nmax_temp_mib = 16\n",
            "schema_version = 1\n[download]\nmax_concurrency = 33\nglobal_max_concurrency = 32\n",
            'schema_version = 1\n[download]\nbackend = "auto"\n',
            'schema_version = 1\n[download.local]\nchunk_size_mib = 4\n',
            'schema_version = 1\n[download.local]\nmax_connections_per_get = 9\n',
            'schema_version = 1\n[download.local]\nmax_connections_per_get = 8\nglobal_max_connections = 4\n',
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for content in invalid:
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(ValueError):
                    load_settings(path)


if __name__ == "__main__":
    unittest.main()

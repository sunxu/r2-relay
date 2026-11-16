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

    def test_local_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "schema_version = 1\\n[upload]\\n"
                "max_part_mib = 64\\n"
                "global_buffer_mib = 256\\n",
                encoding="utf-8",
            )
            settings = load_settings(path)
        self.assertEqual(settings.max_part_mib, 64)
        self.assertEqual(settings.global_buffer_mib, 256)

    def test_rejects_invalid_or_unknown_values(self):
        invalid = (
            "schema_version = 1\\n[upload]\\nmax_part_mib = 0\\n",
            "schema_version = 1\\n[upload]\\nglobal_buffer_mib = 0\\n",
            "schema_version = 1\\n[upload]\\nmax_part_mib = 2048\\nglobal_buffer_mib = 1024\\n",
            "schema_version = 1\\n[upload]\\nunknown = 1\\n",
            "schema_version = 2\\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for content in invalid:
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(ValueError):
                    load_settings(path)


if __name__ == "__main__":
    unittest.main()

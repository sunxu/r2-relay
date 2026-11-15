"""Offline safety checks; no launchctl, network, VPS, or R2 traffic."""
import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / 'ops/macos/manage.py'
spec = importlib.util.spec_from_file_location('r2_relay_mac_manage', MODULE)
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class MacManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = mock.patch.object(manager.Path, 'home', return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.p = manager.paths()
        self.p['config_dir'].mkdir(parents=True, mode=0o700)
        self.p['config_dir'].chmod(0o700)
        self.p['config'].write_text("R2_RELAY_TOKEN='secret'\n")
        self.p['config'].chmod(0o600)

    def test_config_rejects_world_readable_secret(self):
        self.p['config'].chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, '配置必须'):
            manager.check_config(self.p)

    def test_config_rejects_symlink(self):
        original = self.home / 'target'
        original.write_text('secret')
        original.chmod(0o600)
        self.p['config'].unlink()
        self.p['config'].symlink_to(original)
        with self.assertRaisesRegex(RuntimeError, '配置必须'):
            manager.check_config(self.p)

    def test_opaque_cli_and_runner_do_not_embed_token(self):
        self.p['bin'].mkdir(parents=True, exist_ok=True)
        manager.make_runner(self.p)
        content = self.p['run'].read_text()
        self.assertIn('ssl_insecure=false', content)
        self.assertIn('127.0.0.1', content)
        self.assertNotIn('secret', content)
        self.assertEqual(stat.S_IMODE(self.p['run'].stat().st_mode), 0o700)

    def test_activate_requires_explicit_idle_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, 'confirm-idle'):
            manager.activate(self.p, '0' * 40, False)

    def test_activate_refuses_unmanaged_port(self):
        sha = 'a' * 40
        target = self.p['releases'] / sha
        (target / '.venv/bin').mkdir(parents=True)
        (target / 'local').mkdir(parents=True)
        (target / '.venv/bin/mitmdump').touch()
        (target / 'local/addon_entry.py').touch()
        (target / '.deployment-commit').write_text(sha + '\n')
        with mock.patch.object(manager, 'is_loaded', return_value=False), mock.patch.object(manager, 'port_available', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '未受本服务管理'):
                manager.activate(self.p, sha, True)
        self.assertFalse(self.p['current'].exists())

    def test_invalid_release_rejected(self):
        with self.assertRaisesRegex(RuntimeError, '40位'):
            manager.rel_sha(self.p, 'main')

    def test_atomic_symlink_changes_without_deleting_target(self):
        target = self.home / 'release'
        target.mkdir()
        self.p['data'].mkdir(parents=True)
        manager.atomic_link(self.p, 'current', target)
        self.assertEqual(manager.link_target(self.p['current']), target)


if __name__ == '__main__':
    unittest.main()

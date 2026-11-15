"""Offline safety checks; no launchctl, network, VPS, or R2 traffic."""
import importlib.util
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
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

    def _fake_release(self, sha):
        target = self.p['releases'] / sha
        (target / '.venv/bin').mkdir(parents=True)
        (target / 'local').mkdir()
        (target / '.venv/bin/mitmdump').touch()
        (target / 'local/addon_entry.py').touch()
        (target / '.deployment-commit').write_text(sha + "\n")
        return target

    def test_stage_release_builds_venv_at_final_absolute_path(self):
        sha = 'c' * 40
        repo = self.home / 'repo'
        (repo / '.git').mkdir(parents=True)
        seen = []

        def fake_run(cmd, **kwargs):
            values = list(map(str, cmd))
            seen.append(values)
            if 'status' in values:
                return SimpleNamespace(stdout='')
            if 'rev-parse' in values:
                return SimpleNamespace(stdout=sha + '\n')
            if 'archive' in values:
                return SimpleNamespace(stdout=b'fake tar')
            if values[0] == 'tar':
                stage = Path(values[-1])
                (stage / 'local').mkdir()
                (stage / 'local/pyproject.toml').touch()
                (stage / 'local/addon_entry.py').touch()
            if len(values) >= 4 and values[1:3] == ['-m', 'venv']:
                dest = Path(values[3])
                (dest / 'bin').mkdir(parents=True)
                (dest / 'bin/mitmdump').touch()
            return SimpleNamespace(stdout='', returncode=0)

        with mock.patch.object(manager, 'run', side_effect=fake_run), \
                mock.patch.object(manager.shutil, 'which', return_value='/usr/local/bin/python3.11'):
            self.assertEqual(manager.stage_release(repo, sha, self.p), sha)
        target = self.p['releases'] / sha
        self.assertTrue(target.is_dir())
        self.assertEqual((target / '.deployment-commit').read_text().strip(), sha)
        self.assertIn(['/usr/local/bin/python3.11', '-m', 'venv', str(target / '.venv')], seen)
        self.assertIn([str(target / '.venv/bin/python'), '-m', 'pip', 'install', '-e',
                       str(target / 'local')], seen)
        self.assertFalse(any('.source-' in ' '.join(cmd) for cmd in seen
                             if '-m' in cmd and 'venv' in cmd))

    def test_stage_release_failure_cleans_incomplete_release(self):
        sha = 'c' * 40
        repo = self.home / 'repo'
        (repo / '.git').mkdir(parents=True)

        def fake_run(cmd, **kwargs):
            values = list(map(str, cmd))
            if 'status' in values:
                return SimpleNamespace(stdout='')
            if 'rev-parse' in values:
                return SimpleNamespace(stdout=sha + '\n')
            if 'archive' in values:
                return SimpleNamespace(stdout=b'fake tar')
            if values[0] == 'tar':
                stage = Path(values[-1])
                (stage / 'local').mkdir()
                (stage / 'local/pyproject.toml').touch()
                (stage / 'local/addon_entry.py').touch()
            if values[1:3] == ['-m', 'venv']:
                raise subprocess.CalledProcessError(1, cmd)
            return SimpleNamespace(stdout='', returncode=0)

        with mock.patch.object(manager, 'run', side_effect=fake_run), \
                mock.patch.object(manager.shutil, 'which', return_value='/usr/local/bin/python3.11'):
            with self.assertRaises(subprocess.CalledProcessError):
                manager.stage_release(repo, sha, self.p)
        self.assertFalse((self.p['releases'] / sha).exists())

    def test_stage_release_requires_explicit_full_sha(self):
        with self.assertRaisesRegex(RuntimeError, '完整40位'):
            manager.stage_release(self.home, 'HEAD', self.p)

    def test_install_cli_is_independent_of_checkout_path(self):
        sha = 'c' * 40
        self._fake_release(sha)
        args = SimpleNamespace(sha=sha, repo=self.home)
        with mock.patch.object(manager, 'stage_release', return_value=sha), \
                mock.patch.object(manager, 'is_loaded', return_value=False), \
                mock.patch.object(manager.shutil, 'which', return_value='/usr/local/bin/python3.11'):
            manager.install(args, self.p)
        launcher = self.p['cli'].read_text()
        self.assertIn(str(self.p['manager']), launcher)
        self.assertNotIn(str(MODULE), launcher)
        self.assertEqual(self.p['manager'].read_bytes(), MODULE.read_bytes())

    def test_stop_requires_confirm_idle(self):
        with self.assertRaisesRegex(RuntimeError, 'confirm-idle'):
            manager.stop(self.p)

    def test_managed_listener_matches_launchctl_and_lsof_pid(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == 'launchctl':
                return SimpleNamespace(stdout='state = running\n  pid = 4321\n', returncode=0)
            return SimpleNamespace(stdout='4321\n', returncode=0)
        with mock.patch.object(manager, 'is_loaded', return_value=True), \
                mock.patch.object(manager, 'run', side_effect=fake_run):
            self.assertTrue(manager.managed_listener())

    def test_managed_listener_rejects_foreign_pid(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == 'launchctl':
                return SimpleNamespace(stdout='pid = 4321\n', returncode=0)
            return SimpleNamespace(stdout='9999\n', returncode=0)
        with mock.patch.object(manager, 'is_loaded', return_value=True), \
                mock.patch.object(manager, 'run', side_effect=fake_run):
            self.assertFalse(manager.managed_listener())

    def test_installed_link_rejects_outside_release_tree(self):
        outside = self.home / ('d' * 40)
        outside.mkdir()
        self.p['data'].mkdir(parents=True, exist_ok=True)
        self.p['current'].symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, '超出受管理'):
            manager.installed_link(self.p, 'current')

    def test_operation_lock_rejects_parallel_writer(self):
        with manager.operation_lock(self.p):
            with self.assertRaisesRegex(RuntimeError, '另一个'):
                with manager.operation_lock(self.p):
                    pass

    def test_activate_failed_start_restores_previous_release(self):
        previous = self._fake_release('a' * 40)
        updated = self._fake_release('b' * 40)
        self.p['data'].mkdir(parents=True, exist_ok=True)
        self.p['current'].symlink_to(previous)
        with mock.patch.object(manager, 'is_loaded', return_value=True), \
                mock.patch.object(manager, 'stop'), \
                mock.patch.object(manager, 'start', side_effect=[RuntimeError('unhealthy'), None]), \
                mock.patch.object(manager, 'run', return_value=SimpleNamespace(returncode=0)):
            with self.assertRaisesRegex(RuntimeError, 'unhealthy'):
                manager.activate(self.p, updated.name, True)
        self.assertEqual(manager.installed_link(self.p, 'current'), previous)
        self.assertFalse(self.p['previous'].exists())

    def test_uninstall_removes_links_but_keeps_releases_and_secrets(self):
        target = self._fake_release('c' * 40)
        self.p['data'].mkdir(parents=True, exist_ok=True)
        self.p['current'].symlink_to(target)
        for name in ('cli', 'run', 'manager', 'plist'):
            self.p[name].parent.mkdir(parents=True, exist_ok=True)
            self.p[name].touch()
        with mock.patch.object(manager, 'is_loaded', return_value=False):
            manager.uninstall(self.p, True)
        self.assertTrue(target.exists())
        self.assertTrue(self.p['config'].exists())
        self.assertFalse(self.p['current'].is_symlink())
        self.assertFalse(self.p['cli'].exists())

    def test_config_requires_runner_compatible_0600(self):
        self.p['config'].chmod(0o400)
        with self.assertRaisesRegex(RuntimeError, '配置必须'):
            manager.check_config(self.p)


if __name__ == '__main__':
    unittest.main()

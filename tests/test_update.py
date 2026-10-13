"""Updater tests use temporary releases and fake OpenRC; never touch real hardware."""
import argparse
import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('update_ddt', Path(__file__).parents[1] / 'scripts/update_ddt.py')
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)
OLD = 'a' * 40
NEW = 'b' * 40


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.old = self.base / 'releases' / OLD
        self.new = self.base / 'releases' / NEW
        self.old.mkdir(parents=True)
        self.new.mkdir()
        (self.base / 'current').symlink_to(self.old)
        self.config = self.base / 'config.json'
        self.config.write_text('{}')
        self.u = update.Updater(NEW, base=self.base, config=self.config)
        self.u.previous_revision = OLD
        self.u.release = self.new
        self.u.backup = self.base / 'backup'
        self.u.backup.mkdir()
        self.u.original_files = {self.config: b'{}'}
        self.events = []
        self.u.say = lambda message: None
        self.u.snapshot = lambda *args: dict(instance_id='one', restarts=0, valid_rows=1)
        self.u.stop = lambda release: self.events.append(('stop', release))
        self.u.run = lambda args, **kw: self.events.append(tuple(args))
        self.u.wait_ready = lambda release, revision: self.events.append(('ready', release))

    def phase(self):
        return json.loads((self.u.backup / 'transaction.json').read_text())['phase']

    def test_full_commit_only(self):
        self.assertEqual(update.commit_sha('A' * 40), OLD)
        self.assertEqual(update.commit_sha('c' * 64), 'c' * 64)
        for value in ('main', 'v1', 'a' * 7, '../release', '--help', 'g' * 40, 'a' * 41):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                update.commit_sha(value)

    def test_lock_rejects_second_update_and_releases_on_error(self):
        lock = self.base / 'update.lock'
        with self.assertRaises(ValueError):
            with update.update_lock(lock):
                with self.assertRaises(update.UpdateError):
                    with update.update_lock(lock):
                        self.fail('second updater acquired lock')
                raise ValueError('interrupted')
        with update.update_lock(lock):
            self.assertTrue(lock.exists())

    def test_atomic_switch_preserves_both_releases(self):
        update.switch_release(self.u.current, self.new)
        self.assertEqual(self.u.current.resolve(), self.new)
        self.assertTrue(self.old.is_dir())
        self.assertEqual(list(self.base.glob('.switch-*')), [])

    def test_switch_refuses_real_directory(self):
        directory = self.base / 'directory'
        directory.mkdir()
        with self.assertRaises(update.UpdateError):
            update.switch_release(directory, self.new)
        self.assertTrue(directory.is_dir())

    def test_success_stops_before_switch_and_starts_new(self):
        def stop(release):
            self.assertEqual(self.u.current.resolve(), self.old)
            self.events.append(('stop', release))
        self.u.stop = stop
        self.u.activate()
        self.assertEqual(self.u.current.resolve(), self.new)
        self.assertEqual(self.phase(), 'success')
        self.assertEqual(self.events, [('stop', self.old), ('rc-service', 'sweep', 'start'), ('ready', self.new)])
        self.assertEqual(self.config.read_bytes(), b'{}')

    def test_changed_config_or_link_leaves_running_service_alone(self):
        self.config.write_text('{"threshold_db": -30}')
        with self.assertRaises(update.UpdateError):
            self.u.activate()
        self.assertEqual(self.events, [])
        self.config.write_text('{}')
        update.switch_release(self.u.current, self.new)
        with self.assertRaises(update.UpdateError):
            self.u.activate()
        self.assertEqual(self.events, [])

    def test_unready_previous_aborts_before_stop(self):
        self.u.snapshot = lambda *args: (_ for _ in ()).throw(update.UpdateError('not ready'))
        with self.assertRaises(update.UpdateError):
            self.u.activate()
        self.assertEqual(self.events, [])

    def test_start_failure_or_interrupt_rolls_back_and_verifies_old(self):
        for failure in (update.UpdateError('start failed'), KeyboardInterrupt(), update.UpdateError('SIGHUP')):
            with self.subTest(failure=type(failure).__name__):
                self.events.clear()
                def run(args, **kw):
                    self.events.append(tuple(args))
                    if self.u.current.resolve() == self.new:
                        raise failure
                self.u.run = run
                with self.assertRaisesRegex(update.UpdateError, 'previous release restored'):
                    self.u.activate()
                self.assertEqual(self.u.current.resolve(), self.old)
                self.assertEqual(self.phase(), 'rolled_back')
                self.assertIn(('stop', self.new), self.events)
                self.assertEqual(self.events[-1], ('ready', self.old))

    def test_readiness_failure_rolls_back(self):
        def ready(release, revision):
            if release == self.new:
                raise update.UpdateError('no fresh measurements')
            self.events.append(('ready', release))
        self.u.wait_ready = ready
        with self.assertRaisesRegex(update.UpdateError, 'previous release restored'):
            self.u.activate()
        self.assertEqual(self.u.current.resolve(), self.old)
        self.assertEqual(self.phase(), 'rolled_back')

    def test_cannot_stop_new_never_starts_old_in_parallel(self):
        def stop(release):
            if release == self.new:
                raise update.UpdateError('orphan receiver')
        def ready(*args):
            raise update.UpdateError('not ready')
        self.u.stop, self.u.wait_ready = stop, ready
        with self.assertRaisesRegex(update.UpdateError, 'update and rollback failed'):
            self.u.activate()
        self.assertEqual(self.u.current.resolve(), self.new)
        self.assertEqual(self.phase(), 'rollback_failed')
        self.assertEqual(self.events, [('rc-service', 'sweep', 'start')])

    def test_rollback_health_failure_is_reported(self):
        self.u.wait_ready = lambda *args: (_ for _ in ()).throw(update.UpdateError('USB unavailable'))
        with self.assertRaisesRegex(update.UpdateError, 'update and rollback failed'):
            self.u.activate()
        self.assertEqual(self.u.current.resolve(), self.old)
        self.assertEqual(self.phase(), 'rollback_failed')

    def test_stop_verifies_orphan_processes(self):
        with patch.object(update, 'release_processes', return_value=[123]), \
                patch.object(update.time, 'monotonic', side_effect=[0, 6]):
            with self.assertRaisesRegex(update.UpdateError, '123'):
                update.Updater.stop(self.u, self.old)

    def test_ready_requires_stable_instance_and_growing_measurements(self):
        values = [dict(instance_id='one', restarts=0, valid_rows=1),
                  dict(instance_id='two', restarts=0, valid_rows=2),
                  dict(instance_id='two', restarts=0, valid_rows=3)]
        self.u.snapshot = lambda *args: values.pop(0)
        # deadline; loop/first/check; loop/first/check; loop/check
        with patch.object(update.time, 'monotonic', side_effect=[0, 0, 0, 0, 2, 2, 2, 5, 5]), \
                patch.object(update.time, 'sleep'):
            update.Updater.wait_ready(self.u, self.new, NEW, timeout=10, stable_seconds=3)
        self.assertEqual(values, [])

    def test_static_rows_never_pass_readiness(self):
        with patch.object(update.time, 'monotonic', side_effect=range(30)), \
                patch.object(update.time, 'sleep'):
            with self.assertRaisesRegex(update.UpdateError, 'stably ready'):
                update.Updater.wait_ready(self.u, self.new, NEW, timeout=8, stable_seconds=1)

    def test_preparation_failure_does_not_touch_current_or_service(self):
        self.u.repo = 'trusted-test-repository'
        self.u.run = lambda *args, **kw: (_ for _ in ()).throw(update.UpdateError('network failure'))
        with self.assertRaisesRegex(update.UpdateError, 'network failure'):
            self.u.prepare()
        self.assertEqual(self.u.current.resolve(), self.old)
        self.assertTrue(self.u.release.is_dir())  # failed attempt retained, never reused
        self.assertEqual(self.phase(), 'building')

    def test_prepare_pipeline_uses_new_binary_and_does_not_stop_service(self):
        self.u.repo = 'trusted-test-repository'
        self.u.check_service_files = lambda release: None
        def run(args, **kw):
            self.events.append((list(map(str, args)), kw))
            if args[:2] == ['git', 'rev-parse']:
                return NEW
            return ''
        self.u.run = run
        self.u.prepare()
        self.assertEqual(self.phase(), 'prepared')
        self.assertEqual(self.u.current.resolve(), self.old)
        check = next((args, kw) for args, kw in self.events if args[0] == 'make' and args[-1] == 'check')
        self.assertEqual(check[1]['env']['DDT_SWEEP'], str(self.u.release / 'build/bin/sweep'))
        self.assertEqual(check[1]['cwd'], self.u.release)
        self.assertFalse(any(args[0] == 'rc-service' for args, _ in self.events))
        self.assertTrue((self.u.backup / 'manifest.txt').exists())

    def test_wrong_commit_and_test_failure_preserve_running_release(self):
        for failure in ('wrong_commit', 'tests'):
            with self.subTest(failure=failure):
                self.u.repo = 'trusted-test-repository'
                self.u.check_service_files = lambda release: None
                def run(args, **kw):
                    if args[:2] == ['git', 'rev-parse']:
                        return OLD if failure == 'wrong_commit' else NEW
                    if args[0] == 'make' and args[-1] == 'check':
                        raise update.UpdateError('test failure')
                    return ''
                self.u.run = run
                with self.assertRaises(update.UpdateError):
                    self.u.prepare()
                self.assertEqual(self.u.current.resolve(), self.old)

    def test_proc_scan_finds_runtime_and_orphan_sweep_only(self):
        proc = self.base / 'proc'
        proc.mkdir()
        (self.old / 'build/bin').mkdir(parents=True)
        binary = self.old / 'build/bin/sweep'
        binary.touch()
        for pid, executable, cwd, argv in (
                (1, sys.executable, self.old, b'python3\0-m\0ddt\0'),
                (2, binary, self.old, b'sweep\0'),
                (3, sys.executable, self.new, b'python3\0-m\0ddt\0'),
                (4, sys.executable, self.old, b'python3\0some_script.py\0')):
            entry = proc / str(pid)
            entry.mkdir()
            (entry / 'exe').symlink_to(executable)
            (entry / 'cwd').symlink_to(cwd)
            (entry / 'cmdline').write_bytes(argv)
        (proc / '5').mkdir()  # process which exited during enumeration
        self.assertEqual(sorted(update.release_processes(self.old, proc)), [1, 2])

    def test_service_change_requires_manual_migration(self):
        root = Path(__file__).parents[1]
        self.u.init, self.u.conf = self.base / 'init', self.base / 'conf'
        self.u.init.write_bytes((root / 'deploy/openrc/sweep').read_bytes())
        self.u.conf.write_bytes((root / 'deploy/openrc/sweep.conf').read_bytes())
        self.u.check_service_files(root)
        self.u.conf.write_text('project_dir="/root/ddt"\n')
        with self.assertRaisesRegex(update.UpdateError, 'manual migration'):
            self.u.check_service_files(root)

    def test_subprocess_timeout_reaps_child(self):
        self.u.log = (self.base / 'command.log').open('a')
        self.addCleanup(self.u.log.close)
        pidfile = self.base / 'child.pid'
        with self.assertRaises(subprocess.TimeoutExpired):
            update.Updater.run(self.u, [sys.executable, '-c',
                'import os,sys,time; open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(30)',
                pidfile], timeout=0.5)
        pid = int(pidfile.read_text())
        self.assertFalse(Path(f'/proc/{pid}').exists())

    def test_help_does_not_require_root_or_device(self):
        result = subprocess.run([sys.executable, SPEC.origin, '--help'], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'--repo', result.stdout)

    def test_snapshot_checks_live_identity_revision_freshness_and_hardware(self):
        from ddt.__main__ import Runtime
        from ddt.config import Config
        status = Runtime(Config(display_enabled=False, gpio_enabled=False)).snapshot()
        status.update(state='receiving', valid_rows=1, last_measurement_monotonic=time.monotonic())
        status['sdr']['ready'] = True
        status['version'].update(git_revision=NEW, git_dirty=False)
        path = self.base / 'status.json'
        def check(value):
            path.write_text(json.dumps(value))
            # Readiness checks must work even when Python optimizations are enabled.
            return subprocess.run([sys.executable, '-O', '-c', update.SNAPSHOT, str(path), NEW],
                                  capture_output=True, timeout=5)
        self.assertEqual(check(status).returncode, 0)
        for key, value in (
                ('version', dict(git_revision=OLD, git_dirty=False)),
                ('version', dict(git_revision=NEW, git_dirty=True)),
                ('last_measurement_monotonic', time.monotonic() - 60),
                ('boot_id', 'another-boot'),
                ('process_start_ticks', status['process_start_ticks'] + 1),
                ('hardware', dict(display=dict(enabled=True, ready=False),
                                  gpio=dict(enabled=False, ready=False)))):
            with self.subTest(key=key):
                self.assertNotEqual(check(dict(status, **{key: value})).returncode, 0)

    def test_nohup_remains_ignored_and_handlers_are_restored(self):
        original = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGHUP, original)
        self.u.preflight = lambda: None
        self.u.prepare = lambda: self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        self.u.activate = lambda: None
        with patch.object(update.os, 'geteuid', return_value=0), \
                patch.object(update, 'Updater', return_value=self.u), \
                patch.object(update, 'update_lock', return_value=update.contextlib.nullcontext()):
            self.assertEqual(update.main([NEW]), 0)
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)


if __name__ == '__main__':
    unittest.main()

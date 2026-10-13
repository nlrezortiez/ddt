from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ddt.__main__ import LogContext, Runtime
from ddt.config import Config
from ddt.diagnostics import Duration, Rate, ScanCoverage, config_identity, version_info
from ddt.health import healthy
from ddt.process import Receiver
from ddt.spectrum import Measurement
from ddt.status import assess, read_status, readable
from test_spectrum import ROW


class IdentityTests(unittest.TestCase):
    def test_configuration_is_canonical_and_sensitive_to_settings(self):
        default = config_identity(Config())
        self.assertEqual(default, config_identity(replace(Config(), startup_timeout=15)))
        self.assertNotEqual(default['id'], config_identity(replace(Config(), lna_gain=24))['id'])
        self.assertNotEqual(default['id'], config_identity(Config(), -20)['id'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(default['values'], sort_keys=False, indent=4))
            self.assertEqual(default, config_identity(Config.load(path)))

    def test_source_identity_without_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'ddt').mkdir()
            (root / 'ddt/a.py').write_text('a = 1')
            (root / 'requirements.txt').write_text('')
            with patch('ddt.diagnostics.subprocess.run', side_effect=FileNotFoundError):
                first = version_info(root)
                self.assertIsNone(first['git_revision'])
                self.assertIsNone(first['git_dirty'])
                self.assertEqual(first, version_info(root))
                (root / 'ddt/a.py').write_text('a = 2')
                self.assertNotEqual(first['source_sha256'], version_info(root)['source_sha256'])

    def test_log_context_has_monotonic_and_instance(self):
        context = LogContext()
        record = logging.LogRecord('ddt.test', logging.INFO, '', 1, 'example', (), None)
        with patch('ddt.__main__.time.monotonic', return_value=123):
            context.filter(record)
        self.assertEqual(record.monotonic, 123)
        self.assertEqual(record.instance_id, context.instance)
        self.assertEqual(record.boot_id, context.boot)


class MetricTests(unittest.TestCase):
    def test_duration_summary(self):
        timer = Duration()
        self.assertIsNone(timer.snapshot()['mean_ms'])
        timer.observe(.01)
        timer.observe(.03)
        self.assertEqual(timer.snapshot(), dict(count=2, last_ms=30, mean_ms=20, max_ms=30))

    def test_rate_decays_to_zero_and_memory_is_bounded(self):
        rate = Rate(0)
        self.assertEqual(rate.snapshot(1, 100)['rows_per_second'], 100)
        for now in range(2, 100):
            result = rate.snapshot(now, 100)
        self.assertEqual(result['rows_per_second'], 0)
        self.assertLessEqual(len(rate.samples), 12)

    def test_actual_reader_queue_metrics(self):
        receiver = Receiver([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            receiver.rows = queue.Queue(maxsize=2)
            for index in range(5):
                receiver._offer(f'{index}\n', index)
            metrics = receiver.metrics()
            self.assertEqual(metrics['received_rows'], 5)
            self.assertEqual(metrics['overflow_dropped_rows'], 3)
            self.assertEqual(metrics['queue_high_watermark'], 2)
            self.assertEqual(metrics['queue_depth'], 2)
            self.assertEqual(metrics['last_received_monotonic'], 4)
            self.assertEqual(receiver.rows.get_nowait(), (3, '3\n'))
        finally:
            receiver.stop(1)

    def test_consumer_race_does_not_report_phantom_loss(self):
        receiver = Receiver([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            receiver.rows = Mock()
            receiver.rows.put_nowait.side_effect = [queue.Full, None]
            receiver.rows.get_nowait.side_effect = queue.Empty
            receiver.rows.qsize.return_value = 1
            receiver._offer('row\n', 1)
            self.assertEqual(receiver.dropped, 0)
        finally:
            receiver.stop(1)


class ScanTests(unittest.TestCase):
    @staticmethod
    def row(low, high=None):
        return Measurement(low * 1000000, (low + 5 if high is None else high) * 1000000, 1000000, (-50,) * 5)

    def test_complete_interleaved_scan(self):
        scan = ScanCoverage(((0, 20),))
        for now, low in enumerate((0, 10, 5, 15, 0)):
            scan.observe(self.row(low), now)
        self.assertEqual(scan.last['coverage_fraction'], 1)
        self.assertEqual(scan.last['duration_seconds'], 4)
        self.assertEqual(scan.completed, 1)

    def test_gap_and_duplicate_do_not_count_as_complete(self):
        scan = ScanCoverage(((0, 20),))
        for now, low in enumerate((0, 10, 10, 15, 0)):
            scan.observe(self.row(low), now)
        self.assertEqual(scan.last['coverage_fraction'], .75)
        self.assertEqual(scan.incomplete, 1)
        self.assertEqual(scan.completed, 0)

    def test_wait_for_boundary_after_loss_or_reconnect(self):
        scan = ScanCoverage(((0, 20),))
        scan.observe(self.row(10), 0)
        self.assertFalse(scan.snapshot()['synchronized'])
        scan.observe(self.row(0), 1)
        scan.invalidate()
        scan.observe(self.row(10), 2)
        scan.observe(self.row(0), 3)
        self.assertEqual(scan.invalidated, 1)
        self.assertIsNone(scan.last)

    def test_disjoint_ranges_and_ambiguous_tuning(self):
        scan = ScanCoverage(((0, 5), (100, 105)))
        for now, low in enumerate((0, 100, 0)):
            scan.observe(self.row(low), now)
        self.assertEqual(scan.completed, 1)
        self.assertFalse(ScanCoverage(((0, 5), (10, 15))).supported)

    def test_interval_memory_is_bounded(self):
        scan = ScanCoverage(((0, 1),))
        scan.started = 0
        scan.intervals = [(i * 2, i * 2 + 1) for i in range(4096)]
        scan.observe(Measurement(10000, 10001, 1, (-50,)), 1)
        self.assertIsNone(scan.started)
        self.assertEqual(scan.intervals, [])


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'status.json'
        self.config = replace(Config(), status_file=str(self.path), display_enabled=False, gpio_enabled=False)
        self.runtime = Runtime(self.config)

    def publish(self, ready=False):
        if ready:
            self.runtime.receiver = self.fake_receiver()
            self.runtime.consume(ROW)
        self.runtime.tick(force=True)
        return read_status(self.path)

    @staticmethod
    def fake_receiver(received=0, dropped=0, depth=0):
        fake = Mock()
        fake.process.pid = os.getpid()
        fake.process.poll.return_value = None
        fake.eof = threading.Event()
        fake.metrics.return_value = dict(received_rows=received, overflow_dropped_rows=dropped,
                                         reader_errors=0, reader_error=None, last_received_monotonic=time.monotonic(),
                                         queue_depth=depth, queue_capacity=2048, queue_high_watermark=depth)
        return fake

    def test_alive_without_receiver_is_not_ready(self):
        self.publish()
        self.assertTrue(healthy(str(self.path)))
        self.assertFalse(healthy(str(self.path), ready=True))

    def test_readiness_expires_independently_of_status_heartbeat(self):
        status = self.publish(ready=True)
        self.assertTrue(assess(status)['ready'])
        status['last_measurement_monotonic'] -= 10
        self.assertTrue(assess(status)['alive'])
        self.assertFalse(assess(status)['ready'])

    def test_pid_reuse_previous_boot_future_and_stale_status(self):
        status = self.publish(ready=True)
        for field, value in (('process_start_ticks', status['process_start_ticks'] + 1),
                             ('boot_id', 'previous-boot'),
                             ('updated_monotonic', time.monotonic() - 30),
                             ('updated_monotonic', time.monotonic() + 30)):
            with self.subTest(field=field):
                self.assertFalse(assess(dict(status, **{field: value}))['alive'])
        with patch('ddt.status.process_identity', return_value=(status['process_start_ticks'], 'Z')):
            self.assertFalse(assess(status)['alive'])

    def test_hardware_degradation_does_not_change_sdr_readiness(self):
        status = self.publish(ready=True)
        status['hardware']['display'].update(enabled=True, ready=False)
        result = assess(status)
        self.assertTrue(result['ready'])
        self.assertFalse(result['device_ready'])
        self.assertIn('display_unavailable', result['warnings'])

    def test_replay_and_stopped_are_never_ready(self):
        status = self.publish(ready=True)
        status['mode'] = 'replay'
        self.assertFalse(assess(status)['ready'])
        self.runtime.finish()
        self.assertFalse(healthy(str(self.path)))

    def test_cumulative_losses_survive_two_receiver_restarts(self):
        self.runtime.receiver = self.fake_receiver(10, 3, 2)
        self.runtime.retire_receiver()
        self.runtime.receiver = self.fake_receiver(20, 4, 1)
        self.runtime.retire_receiver()
        result = self.runtime.snapshot()['metrics']
        self.assertEqual(result['received_rows'], 30)
        self.assertEqual(result['overflow_dropped_rows'], 7)
        self.assertEqual(result['shutdown_discarded_rows'], 3)
        self.assertEqual(result['queue']['depth'], 0)

    def test_transition_history_is_bounded_and_failure_survives_recovery(self):
        self.runtime.failure('data_timeout', 'test silence')
        self.runtime.consume(ROW)
        self.assertEqual(self.runtime.last_failure['code'], 'data_timeout')
        self.assertEqual(self.runtime.error, '')
        count = self.runtime.transition_count
        for _ in range(100):
            self.runtime.consume(ROW)
        self.assertEqual(self.runtime.transition_count, count)
        for i in range(100):
            self.runtime.transition('reconnecting', f'test-{i}', force=True)
        self.assertEqual(len(self.runtime.transitions), 32)

    def test_button_changes_effective_not_startup_config_id(self):
        startup = self.runtime.startup_config_id
        self.runtime.hardware._change(1)
        self.runtime.tick()
        status = read_status(self.path)
        self.assertEqual(status['startup_config_id'], startup)
        self.assertNotEqual(status['configuration']['id'], startup)
        self.assertEqual(status['configuration']['values']['threshold_db'], self.config.threshold_db + 1)

    def test_first_data_time_queue_latency_and_processing_metrics(self):
        self.runtime.started = 100
        self.runtime.clock = Mock(side_effect=[102, 102.005, 102.01])
        self.runtime.consume(ROW, received_at=101.5)
        self.assertEqual(self.runtime.first_measurement_after, 1.5)
        self.assertEqual(self.runtime.queue_latency.snapshot()['last_ms'], 500)
        self.assertAlmostEqual(self.runtime.processing_duration.snapshot()['last_ms'], 10)

    def test_status_write_is_throttled_but_transition_is_immediate(self):
        self.runtime.clock = Mock(return_value=self.runtime.started)
        with patch('ddt.__main__.Path.replace') as write:
            self.runtime.tick()
            self.runtime.tick()
            self.assertEqual(write.call_count, 1)
            self.runtime.transition('waiting for SDR', 'receiver_start')
            self.runtime.tick()
            self.assertEqual(write.call_count, 2)

    def test_cli_is_read_only_and_machine_readable(self):
        status = self.publish(ready=True)
        before = self.path.read_bytes()
        result = subprocess.run([sys.executable, '-m', 'ddt', 'status', '--file', str(self.path), '--json'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['snapshot']['instance_id'], status['instance_id'])
        self.assertEqual(before, self.path.read_bytes())
        self.assertIn('overflow=', readable(status, assess(status)))

    def test_wrapper_status_and_help_do_not_start_runtime(self):
        self.publish(ready=True)
        env = dict(os.environ, DDT_PROJECT_DIR=str(Path.cwd()), DDT_PYTHON=sys.executable)
        result = subprocess.run(['sh', 'deploy/ddt', 'status', '--file', str(self.path), '--json'],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['assessment']['ready'])
        result = subprocess.run(['sh', 'deploy/ddt'], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage:', result.stdout)

    def test_cli_unavailable_and_malformed_status(self):
        for content in ('not json', '{}', '{"schema_version": 1}'):
            self.path.write_text(content)
            self.assertFalse(healthy(str(self.path)))
            result = subprocess.run([sys.executable, '-m', 'ddt', 'status', '--file', str(self.path), '--json'],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(json.loads(result.stdout)['assessment']['alive'])
        status = self.publish()
        for key, value in (('pid', -1), ('updated_monotonic', float('nan')), ('hardware', [])):
            self.path.write_text(json.dumps(dict(status, **{key: value})))
            self.assertFalse(healthy(str(self.path)))

    def test_status_write_failure_still_cleans_up_receiver(self):
        fake = self.fake_receiver()
        fake.rows = queue.Queue()
        with patch('ddt.__main__.Receiver', return_value=fake), \
                patch.object(self.runtime, 'tick', side_effect=[None, OSError('write failed'), OSError('write failed')]), \
                self.assertRaises(OSError):
            self.runtime.run()
        fake.stop.assert_called_once_with(self.config.stop_timeout)
        self.assertIsNone(self.runtime.receiver)

    def test_live_status_and_recovery_with_real_subprocess(self):
        self.runtime = Runtime(replace(self.config, retry_delay=.1, retry_max_delay=.2))
        source = f'import time\nwhile True:\n print({ROW!r}, end="", flush=True); time.sleep(.02)'
        errors = []
        def run():
            try:
                self.runtime.run()
            except Exception as exc:
                errors.append(exc)
        def wait_for(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    status = read_status(self.path)
                    if predicate(status):
                        return status
                except (OSError, ValueError):
                    pass
                time.sleep(.02)
            self.fail(f'status did not reach expected state, errors={errors}')
        with patch.object(Config, 'command', return_value=[sys.executable, '-c', source]):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                first = wait_for(lambda status: assess(status)['ready'])
                self.runtime.receiver.process.terminate()
                recovered = wait_for(lambda status: assess(status)['ready'] and status['sdr']['pid'] != first['sdr']['pid'])
                self.assertGreaterEqual(recovered['restarts'], 1)
                self.assertGreater(recovered['metrics']['received_rows'], first['metrics']['received_rows'])
                self.assertIn(recovered['last_failure']['code'], ('receiver_exit', 'stdout_eof'))
                self.assertEqual(recovered['instance_id'], first['instance_id'])
            finally:
                self.runtime.stop_event.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()

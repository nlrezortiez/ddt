from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ddt.__main__ import Runtime
from ddt.config import Config
from ddt.health import healthy
from ddt.hardware import Hardware
from ddt.process import Receiver
from test_spectrum import ROW


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(Config(), display_enabled=False, gpio_enabled=False,
                              status_file=str(Path(self.temp.name) / 'status.json'),
                              startup_timeout=.12, data_timeout=.12, retry_delay=.1,
                              retry_max_delay=.2, stop_timeout=.2)

    def test_replay_first_row_not_discarded(self):
        path = Path(self.temp.name) / 'recording.csv'
        path.write_text(ROW)
        runtime = Runtime(self.config)
        self.assertEqual(runtime.replay(path), 0)
        self.assertEqual(runtime.valid_rows, 1)
        self.assertTrue(healthy(self.config.status_file, 15))
        runtime.state = 'stopped'
        runtime.tick()
        self.assertFalse(healthy(self.config.status_file, 15))

    def test_bad_rows_do_not_refresh_freshness(self):
        runtime = Runtime(self.config)
        runtime.consume(ROW, received_at=100)
        runtime.consume('broken')
        self.assertEqual(runtime.last_valid, 100)
        self.assertEqual(runtime.invalid_rows, 1)

    def test_eof_silence_and_malformed_stream_restart(self):
        for source in ('pass', 'import time; time.sleep(30)',
                       'import time\nwhile True:\n print("broken", flush=True); time.sleep(.01)'):
            runtime = Runtime(self.config)
            timer = threading.Timer(.65, runtime.stop_event.set)
            timer.start()
            started = time.monotonic()
            try:
                with self.subTest(source=source), patch.object(Config, 'command', return_value=[sys.executable, '-c', source]):
                    self.assertEqual(runtime.run(), 0)
                    self.assertGreaterEqual(runtime.restarts, 1)
                    self.assertLess(time.monotonic() - started, 2)
            finally:
                runtime.stop_event.set()
                timer.join()

    def test_healthy_stream(self):
        source = f'import time\nwhile True:\n print({ROW!r}, end="", flush=True); time.sleep(.02)'
        runtime = Runtime(replace(self.config, startup_timeout=1, data_timeout=1))
        timer = threading.Timer(1.2, runtime.stop_event.set)
        timer.start()
        try:
            with patch.object(Config, 'command', return_value=[sys.executable, '-c', source]):
                runtime.run()
            self.assertGreater(runtime.valid_rows, 0)
            self.assertEqual(runtime.restarts, 0)
        finally:
            timer.join()

    def test_health_stale_corrupt_missing(self):
        path = Path(self.config.status_file)
        self.assertFalse(healthy(str(path), 15))
        path.write_text('bad json')
        self.assertFalse(healthy(str(path), 15))
        path.write_text(json.dumps(dict(pid=os.getpid(), state='receiving', updated_monotonic=time.monotonic() - 30)))
        self.assertFalse(healthy(str(path), 15))

    def test_headless_never_imports_hardware_libraries(self):
        runtime = Runtime(self.config)
        runtime.tick()
        self.assertNotIn('luma.oled.device', sys.modules)
        self.assertNotIn('lgpio', sys.modules)

    def test_gpio_failure_does_not_prevent_tick(self):
        hardware = Hardware(replace(self.config, gpio_enabled=True))
        with patch.dict(sys.modules, {'lgpio': None}):
            hardware.tick('starting', -32, None)
        self.assertIsNone(hardware.buttons)
        self.assertGreater(hardware.next_gpio, time.monotonic())

    def test_display_failure_is_retried_without_stopping_receiver(self):
        hardware = Hardware(replace(self.config, display_enabled=True))
        with patch.dict(sys.modules, {'luma': None}):
            hardware.tick('receiving', -32, None)
        self.assertIsNone(hardware.device)
        self.assertGreater(hardware.next_display, time.monotonic())

    def test_valid_then_stalled_stream_recovers(self):
        source = f'import time; print({ROW!r}, end="", flush=True); time.sleep(30)'
        runtime = Runtime(replace(self.config, startup_timeout=1))
        timer = threading.Timer(1.2, runtime.stop_event.set)
        timer.start()
        try:
            with patch.object(Config, 'command', return_value=[sys.executable, '-c', source]):
                runtime.run()
            self.assertGreater(runtime.valid_rows, 0)
            self.assertGreater(runtime.restarts, 0)
        finally:
            timer.join()

    def test_cli_sigterm_and_single_instance(self):
        cfg = Path(self.temp.name) / 'config.json'
        cfg.write_text(json.dumps({'sweep_binary': '/does/not/exist'}))
        command = [sys.executable, '-m', 'ddt', '--headless', '--config', str(cfg),
                   '--status-file', self.config.status_file]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 3
            while not Path(self.config.status_file).exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertIsNone(process.poll())
            duplicate = subprocess.run(command, capture_output=True, timeout=3)
            self.assertEqual(duplicate.returncode, 2)
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=3), 0)
            self.assertEqual(json.loads(Path(self.config.status_file).read_text())['state'], 'stopped')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


class ProcessTests(unittest.TestCase):
    def test_ignored_term_is_killed(self):
        receiver = Receiver([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready",flush=True); time.sleep(30)'])
        try:
            receiver.rows.get(timeout=3)
            started = time.monotonic()
            receiver.stop(.2)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(receiver.process.returncode, -signal.SIGKILL)
            self.assertFalse(any(t.is_alive() for t in receiver.threads))
        finally:
            if receiver.process.poll() is None:
                receiver.stop(.2)

    def test_oversized_row_ends_reader(self):
        receiver = Receiver([sys.executable, '-c', 'print("x" * 300000, flush=True)'])
        try:
            self.assertTrue(receiver.eof.wait(3))
        finally:
            receiver.stop(.2)

    def test_stderr_drained_and_queue_bounded(self):
        with patch('ddt.process.LOG'):
            receiver = Receiver([sys.executable, '-c', 'import sys\nfor i in range(4000):\n print("row",flush=True); print("e" * 100, file=sys.stderr,flush=True)'])
            try:
                self.assertTrue(receiver.eof.wait(5))
                self.assertLessEqual(receiver.rows.qsize(), 2048)
                self.assertGreater(receiver.dropped, 0)
            finally:
                receiver.stop(.2)

import argparse
from collections import deque
from dataclasses import replace
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time
import uuid

from .config import Config
from .diagnostics import (Duration, Rate, ScanCoverage, boot_id, config_identity,
                          file_sha256, process_identity, version_info)
from .hardware import Hardware
from .process import Receiver
from .spectrum import decode, detect
from .status import SCHEMA_VERSION

LOG = logging.getLogger(__name__)


class LogContext(logging.Filter):
    def __init__(self):
        super().__init__()
        self.boot = boot_id()
        self.instance = uuid.uuid4().hex

    def filter(self, record):
        record.boot_id = self.boot
        record.instance_id = self.instance
        record.monotonic = time.monotonic()
        return True


class ReceiverFailure(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Runtime:
    def __init__(self, config: Config, *, clock=None, context=None):
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.config = config
        self.stop_event = threading.Event()
        self.stop_reason = 'requested_stop'
        self.hardware = Hardware(config)
        self.threshold = config.threshold_db
        self.context = context or LogContext()
        self.process_start_ticks = process_identity(os.getpid())[0]
        self.version = version_info()
        self.binary = dict(path=str(Path(config.sweep_binary).resolve()),
                           sha256=file_sha256(config.sweep_binary))
        self.configuration = config_identity(config)
        self.startup_config_id = self.configuration['id']
        self.mode = 'live'
        self.state = 'starting'
        self.transitions = deque(maxlen=32)
        self.transition_count = 0
        self.detection = None
        self.alert_until = 0.0
        self.last_valid = None
        self.first_measurement_after = None
        self.valid_rows = 0
        self.invalid_rows = 0
        self.restarts = 0
        self.attempts = 0
        self.error = ''
        self.last_failure = None
        self.receiver = None
        self.last_receiver_pid = None
        self.last_exit_code = None
        self.closed_metrics = dict(received_rows=0, overflow_dropped_rows=0, reader_errors=0)
        self.shutdown_discarded = 0
        self.queue_high_watermark = 0
        self.last_received = None
        self.observed_drops = 0
        self.received_rate = Rate(self.started)
        self.valid_rate = Rate(self.started)
        self.processing_duration = Duration()
        self.queue_latency = Duration()
        self.hardware_duration = Duration()
        self.scan = ScanCoverage(config.ranges_mhz)
        self.last_status_at = None
        self.last_status_state = None
        self.status_dirty = True
        self.transition('starting', 'startup', force=True)

    def transition(self, state, reason, *, force=False):
        if self.state == state and not force:
            return
        event = dict(previous=self.state if self.transition_count else None,
                     state=state, reason=reason, monotonic=self.clock())
        self.transition_count += 1
        self.transitions.append(event)
        self.state = state
        self.status_dirty = True
        LOG.info('state_transition previous=%s state=%s reason=%s', event['previous'], state, reason)

    def consume(self, line, received_at=None):
        started = self.clock()
        if received_at is not None:
            self.queue_latency.observe(started - received_at)
        try:
            try:
                measurement = decode(line)
            except ValueError:
                self.invalid_rows += 1
                self.scan.invalidate()
                if self.invalid_rows == 1 or self.invalid_rows % 100 == 0:
                    LOG.warning('invalid receiver row count=%s', self.invalid_rows)
                return
            if measurement is None:
                return
            self.last_valid = started if received_at is None else received_at
            if self.first_measurement_after is None:
                self.first_measurement_after = max(0, self.last_valid - self.started)
            self.valid_rows += 1
            self.transition('receiving', 'valid_measurement')
            self.error = ''
            self.scan.observe(measurement, self.last_valid)
            found = detect(measurement, self.threshold, self.config.excluded_hz)
            if found is not None:
                self.detection = found
                self.alert_until = self.last_valid + self.config.alert_hold
        finally:
            self.processing_duration.observe(self.clock() - started)

    def transport_metrics(self):
        current = self.receiver.metrics() if self.receiver else {}
        totals = {key: value + current.get(key, 0) for key, value in self.closed_metrics.items()}
        self.queue_high_watermark = max(self.queue_high_watermark, current.get('queue_high_watermark', 0))
        if current.get('last_received_monotonic') is not None:
            self.last_received = current['last_received_monotonic']
        if totals['overflow_dropped_rows'] != self.observed_drops:
            self.scan.invalidate()
            self.observed_drops = totals['overflow_dropped_rows']
        return dict(**totals, shutdown_discarded_rows=self.shutdown_discarded,
                    last_received_monotonic=self.last_received,
                    queue=dict(depth=current.get('queue_depth', 0), capacity=current.get('queue_capacity', 2048),
                               high_watermark=self.queue_high_watermark))

    def snapshot(self):
        now = self.clock()
        metrics = self.transport_metrics()
        metrics['received_rate'] = self.received_rate.snapshot(now, metrics['received_rows'])
        metrics['valid_rate'] = self.valid_rate.snapshot(now, self.valid_rows)
        hardware = self.hardware.diagnostics()
        age = None if self.last_valid is None else now - self.last_valid
        receiver_alive = (self.receiver is not None and self.receiver.process.poll() is None
                          and not self.receiver.eof.is_set())
        ready = (self.mode == 'live' and self.state == 'receiving' and receiver_alive
                 and age is not None and 0 <= age <= self.config.data_timeout)
        return dict(
            schema_version=SCHEMA_VERSION, pid=os.getpid(), process_start_ticks=self.process_start_ticks,
            boot_id=self.context.boot, instance_id=self.context.instance, mode=self.mode,
            version=self.version, binary=self.binary, configuration=self.configuration,
            startup_config_id=self.startup_config_id, state=self.state,
            started_monotonic=self.started, updated_monotonic=now, uptime_seconds=max(0, now - self.started),
            first_measurement_after_seconds=self.first_measurement_after,
            last_measurement_monotonic=self.last_valid, threshold_db=self.threshold,
            valid_rows=self.valid_rows, invalid_rows=self.invalid_rows, restarts=self.restarts, error=self.error,
            display_ready=hardware['display']['ready'], gpio_ready=hardware['gpio']['ready'],
            sdr=dict(ready=bool(ready), process_alive=receiver_alive,
                     pid=self.receiver.process.pid if self.receiver else None,
                     last_pid=self.last_receiver_pid, last_exit_code=self.last_exit_code,
                     start_attempts=self.attempts, data_timeout_seconds=self.config.data_timeout),
            metrics=metrics, hardware=hardware, scan=self.scan.snapshot(),
            timings=dict(csv_processing=self.processing_duration.snapshot(),
                         queue_latency=self.queue_latency.snapshot(), hardware_tick=self.hardware_duration.snapshot()),
            last_failure=self.last_failure, transition_count=self.transition_count, transitions=list(self.transitions))

    def tick(self, *, force=False):
        change = self.hardware.threshold_change()
        if change:
            previous_id = self.configuration['id']
            self.threshold += change
            self.configuration = config_identity(self.config, self.threshold)
            self.status_dirty = True
            LOG.info('configuration_change source=buttons previous=%s current=%s threshold_db=%s',
                     previous_id, self.configuration['id'], self.threshold)
        if self.clock() >= self.alert_until or self.state != 'receiving':
            self.detection = None
        if self.state not in ('stopping', 'stopped'):
            started = self.clock()
            self.hardware.tick(self.state, self.threshold, self.detection)
            self.hardware_duration.observe(self.clock() - started)
        now = self.clock()
        if (force or self.status_dirty or self.state != self.last_status_state
                or self.last_status_at is None or now - self.last_status_at >= 1):
            path = Path(self.config.status_file)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.snapshot(), allow_nan=False) + '\n')
            temporary.replace(path)
            self.last_status_at = now
            self.last_status_state = self.state
            self.status_dirty = False

    def retire_receiver(self):
        if self.receiver is None:
            return
        try:
            self.receiver.stop(self.config.stop_timeout)
        finally:
            metrics = self.receiver.metrics()
            self.transport_metrics()
            for key in self.closed_metrics:
                self.closed_metrics[key] += metrics[key]
            self.shutdown_discarded += metrics['queue_depth']
            self.last_exit_code = self.receiver.process.poll()
            self.receiver = None
            self.scan.invalidate()

    def failure(self, code, message):
        self.error = str(message)[:512]
        self.last_failure = dict(code=code, message=self.error, monotonic=self.clock(),
                                 receiver_pid=self.receiver.process.pid if self.receiver else None,
                                 exit_code=self.receiver.process.poll() if self.receiver else None)
        LOG.error('receiver_failure code=%s message=%s', code, self.error)
        self.transition('reconnecting', code, force=True)

    def run(self):
        delay = self.config.retry_delay
        self.tick()
        while not self.stop_event.is_set():
            self.transition('waiting for SDR', 'receiver_start')
            self.last_valid = None
            self.attempts += 1
            started = self.clock()
            try:
                LOG.info('starting receiver command=%s', self.config.command())
                self.receiver = Receiver(self.config.command())
                self.last_receiver_pid = self.receiver.process.pid
                while not self.stop_event.is_set():
                    self.transport_metrics()
                    for _ in range(256):
                        try:
                            received_at, line = self.receiver.rows.get_nowait()
                            self.consume(line, received_at)
                        except queue.Empty:
                            break
                    self.transport_metrics()
                    current = self.receiver.metrics()
                    if current['reader_error']:
                        raise ReceiverFailure('reader_error', current['reader_error'])
                    if self.receiver.process.poll() is not None:
                        raise ReceiverFailure('receiver_exit', f'receiver ended exit={self.receiver.process.poll()}')
                    if self.receiver.eof.is_set():
                        raise ReceiverFailure('stdout_eof', 'receiver output ended')
                    timeout = self.config.startup_timeout if self.last_valid is None else self.config.data_timeout
                    if self.clock() - (started if self.last_valid is None else self.last_valid) > timeout:
                        raise ReceiverFailure('startup_timeout' if self.last_valid is None else 'data_timeout',
                                              f'no valid measurements for {timeout}s')
                    if self.last_valid is not None:
                        delay = self.config.retry_delay
                    self.tick()
                    self.stop_event.wait(.05)
            except (OSError, ReceiverFailure) as exc:
                self.failure(exc.code if isinstance(exc, ReceiverFailure) else 'start_or_io_error', exc)
                self.tick(force=True)
            finally:
                try:
                    if self.stop_event.is_set():
                        self.transition('stopping', self.stop_reason)
                        self.tick(force=True)
                finally:
                    try:
                        self.retire_receiver()
                    except RuntimeError as exc:
                        self.failure('cleanup_timeout', exc)
                        raise
            if self.stop_event.is_set():
                break
            self.restarts += 1
            self.status_dirty = True
            deadline = self.clock() + delay
            while not self.stop_event.is_set() and self.clock() < deadline:
                self.tick()
                self.stop_event.wait(.1)
            delay = min(delay * 2, self.config.retry_max_delay)
        return 0

    def replay(self, path):
        self.mode = 'replay'
        with open(path, encoding='utf-8') as stream:
            for line in stream:
                if self.stop_event.is_set():
                    break
                self.consume(line)
        self.tick(force=True)
        LOG.info('replay complete valid=%s invalid=%s', self.valid_rows, self.invalid_rows)
        return 1 if self.invalid_rows or not self.valid_rows else 0

    def finish(self):
        self.transition('stopped', self.stop_reason)
        try:
            self.hardware.close()
        finally:
            self.tick(force=True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == 'status':
        from .status import main as status_main
        return status_main(argv[1:])
    cli = argparse.ArgumentParser(description='Threshold spectrum detector; use "status --help" for diagnostics')
    cli.add_argument('--config')
    cli.add_argument('--headless', action='store_true', help='disable OLED and GPIO')
    cli.add_argument('--replay', help='process a CSV recording once without starting SDR')
    cli.add_argument('--status-file')
    cli.add_argument('--log-file', help='rotating log: 2 MiB plus three backups')
    cli.add_argument('--version', action='store_true')
    args = cli.parse_args(argv)
    if args.version:
        print(json.dumps(version_info()))
        return 0
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    try:
        config = Config.load(args.config)
        if args.headless:
            config = replace(config, display_enabled=False, gpio_enabled=False)
        if args.status_file:
            config = replace(config, status_file=args.status_file)
        config.validate()
        status = Path(config.status_file)
        status.parent.mkdir(parents=True, exist_ok=True)
        with status.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            context = LogContext()
            handler = (RotatingFileHandler(args.log_file, maxBytes=2 * 1024 * 1024, backupCount=3,
                                           encoding='utf-8') if args.log_file else logging.StreamHandler())
            handler.addFilter(context)
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s boot=%(boot_id)s run=%(instance_id)s mono=%(monotonic).3f %(message)s'))
            logging.getLogger().handlers = [handler]
            runtime = Runtime(config, context=context)
            LOG.info('runtime_identity version=%s config_id=%s binary=%s', runtime.version,
                     runtime.startup_config_id, runtime.binary)
            def stop(signum, _frame):
                runtime.stop_reason = signal.Signals(signum).name
                runtime.stop_event.set()
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, stop)
            try:
                result = runtime.replay(args.replay) if args.replay else runtime.run()
                if args.replay and not runtime.stop_event.is_set():
                    runtime.stop_reason = 'replay_completed' if result == 0 else 'replay_invalid'
                return result
            except Exception:
                runtime.stop_reason = 'runtime_error'
                raise
            finally:
                runtime.finish()
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        LOG.error('runtime failed: %s', exc)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

import argparse
from dataclasses import replace
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import signal
import threading
import time

from .config import Config
from .hardware import Hardware
from .process import Receiver
from .spectrum import decode, detect

LOG = logging.getLogger(__name__)


class Runtime:
    def __init__(self, config: Config):
        self.config = config
        self.stop_event = threading.Event()
        self.hardware = Hardware(config)
        self.threshold = config.threshold_db
        self.state = "starting"
        self.detection = None
        self.alert_until = 0.0
        self.last_valid = None
        self.valid_rows = 0
        self.invalid_rows = 0
        self.restarts = 0
        self.error = ""

    def consume(self, line, received_at=None):
        try:
            measurement = decode(line)
        except ValueError:
            self.invalid_rows += 1
            if self.invalid_rows == 1 or self.invalid_rows % 100 == 0:
                LOG.warning("invalid receiver row count=%s", self.invalid_rows)
            return
        if measurement is None:
            return
        self.last_valid = time.monotonic() if received_at is None else received_at
        self.valid_rows += 1
        self.state = "receiving"
        found = detect(measurement, self.threshold, self.config.excluded_hz)
        if found is not None:
            self.detection = found
            self.alert_until = self.last_valid + self.config.alert_hold

    def tick(self):
        self.threshold += self.hardware.threshold_change()
        if time.monotonic() >= self.alert_until or self.state != "receiving":
            self.detection = None
        if self.state != "stopped":
            self.hardware.tick(self.state, self.threshold, self.detection)
        path = Path(self.config.status_file)
        status = dict(pid=os.getpid(), state=self.state, updated_monotonic=time.monotonic(),
                      last_measurement_monotonic=self.last_valid, threshold_db=self.threshold,
                      valid_rows=self.valid_rows, invalid_rows=self.invalid_rows,
                      restarts=self.restarts, error=self.error,
                      display_ready=self.hardware.device is not None,
                      gpio_ready=bool(self.hardware.buttons))
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(status) + "\n")
        temporary.replace(path)

    def run(self):
        delay = self.config.retry_delay
        self.tick()  # Render boot state even before a receiver is available.
        while not self.stop_event.is_set():
            receiver = None
            self.state = "waiting for SDR"
            self.last_valid = None
            started = time.monotonic()
            try:
                LOG.info("starting receiver command=%s", self.config.command())
                receiver = Receiver(self.config.command())
                while not self.stop_event.is_set():
                    self.tick()
                    for _ in range(256):
                        try:
                            received_at, line = receiver.rows.get_nowait()
                            self.consume(line, received_at)
                        except queue.Empty:
                            break
                    if receiver.process.poll() is not None or receiver.eof.is_set():
                        raise RuntimeError(f"receiver ended exit={receiver.process.poll()}")
                    now = time.monotonic()
                    timeout = self.config.startup_timeout if self.last_valid is None else self.config.data_timeout
                    if now - (started if self.last_valid is None else self.last_valid) > timeout:
                        raise RuntimeError(f"no valid measurements for {timeout}s")
                    if self.last_valid is not None:
                        delay = self.config.retry_delay
                        self.error = ""
                    self.stop_event.wait(0.05)
            except (OSError, RuntimeError) as exc:
                self.error = str(exc)
                LOG.error("receiver failure: %s", exc)
                self.state = "reconnecting"
                self.tick()  # Clear stale alerts before potentially slow cleanup.
            finally:
                if receiver is not None:
                    receiver.stop(self.config.stop_timeout)
            if self.stop_event.is_set():
                break
            self.state = "reconnecting"
            self.restarts += 1
            deadline = time.monotonic() + delay
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                self.tick()
                self.stop_event.wait(0.1)
            delay = min(delay * 2, self.config.retry_max_delay)
        return 0

    def replay(self, path):
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                if self.stop_event.is_set():
                    break
                self.consume(line)
        self.tick()
        LOG.info("replay complete valid=%s invalid=%s", self.valid_rows, self.invalid_rows)
        return 1 if self.invalid_rows or not self.valid_rows else 0


def main():
    cli = argparse.ArgumentParser(description="Threshold spectrum detector")
    cli.add_argument("--config")
    cli.add_argument("--headless", action="store_true", help="disable OLED and GPIO")
    cli.add_argument("--replay", help="process a CSV recording once without starting SDR")
    cli.add_argument("--status-file")
    cli.add_argument("--log-file", help="rotating log: 2 MiB plus three backups")
    args = cli.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if args.log_file:
            handler = RotatingFileHandler(args.log_file, maxBytes=2 * 1024 * 1024,
                                          backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            logging.getLogger().handlers = [handler]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        LOG.info("boot_id=%s monotonic=%.3f", boot_id, time.monotonic())
        config = Config.load(args.config)
        if args.headless:
            config = replace(config, display_enabled=False, gpio_enabled=False)
        if args.status_file:
            config = replace(config, status_file=args.status_file)
        config.validate()
        status = Path(config.status_file)
        status.parent.mkdir(parents=True, exist_ok=True)
        with status.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            runtime = Runtime(config)
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: runtime.stop_event.set())
            try:
                return runtime.replay(args.replay) if args.replay else runtime.run()
            finally:
                runtime.state = "stopped"
                try:
                    runtime.tick()
                finally:
                    runtime.hardware.close()
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        LOG.error("startup failed: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

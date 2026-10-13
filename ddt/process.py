"""Bounded pipe readers and cooperative receiver shutdown."""

import logging
import os
import queue
import signal
import subprocess
import threading
import time

LOG = logging.getLogger(__name__)


class Receiver:
    def __init__(self, command: list[str]):
        self.rows: queue.Queue[tuple[float, str]] = queue.Queue(maxsize=2048)
        self.dropped = 0
        self.eof = threading.Event()
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        start_new_session=True, text=True, encoding="utf-8",
                                        errors="replace", bufsize=1)
        self.threads = [threading.Thread(target=self._stdout, daemon=True),
                        threading.Thread(target=self._stderr, daemon=True)]
        for thread in self.threads:
            thread.start()

    def _stdout(self) -> None:
        try:
            while True:
                line = self.process.stdout.readline(262144)
                if not line:
                    break
                if not line.endswith("\n"):
                    raise ValueError("oversized or incomplete receiver row")
                try:
                    self.rows.put_nowait((time.monotonic(), line))
                except queue.Full:
                    try:
                        self.rows.get_nowait()
                    except queue.Empty:
                        pass
                    self.dropped += 1
                    self.rows.put_nowait((time.monotonic(), line))
        except (OSError, ValueError):
            LOG.exception("receiver output reader failed")
        finally:
            self.eof.set()

    def _stderr(self) -> None:
        try:
            for line in iter(lambda: self.process.stderr.readline(4096), ""):
                LOG.warning("sweep: %s", line.rstrip())
        except (OSError, ValueError):
            LOG.exception("receiver stderr reader failed")

    def stop(self, timeout: float) -> None:
        # Signal the whole session, including children holding pipes after the
        # leader exits. One deadline per phase, not one timeout per thread.
        LOG.info("stopping sweep pid=%s", self.process.pid)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            deadline = time.monotonic() + timeout
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            for thread in self.threads:
                thread.join(timeout=max(0, deadline - time.monotonic()))
            if self.process.poll() is not None and not any(t.is_alive() for t in self.threads):
                break
        else:
            # Do not accumulate unkillable receivers; let the service fail.
            raise RuntimeError("receiver cleanup timed out (possible blocked kernel I/O)")
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()
        LOG.info("sweep exit=%s dropped_rows=%s", self.process.returncode, self.dropped)

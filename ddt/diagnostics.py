"""Bounded, hardware-independent telemetry. All durations are monotonic."""
from collections import deque
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def process_identity(pid):
    # comm may contain spaces and parentheses; fields after the last ')' start
    # at proc(5) field 3. starttime is field 22.
    tail = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return int(tail[19]), tail[0]


def file_sha256(path):
    try:
        with open(path, 'rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    except OSError:
        return None


def version_info(root=None):
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    files = sorted([*root.glob('ddt/*.py'), *root.glob('src/*.c'),
                    *root.glob('libhackrf/src/*.[ch]'), root / 'requirements.txt'])
    for path in files:
        digest.update(str(path.relative_to(root)).encode() + b'\0')
        digest.update(path.read_bytes() + b'\0')
    result = dict(source_sha256=digest.hexdigest(), git_revision=None, git_dirty=None)
    try:
        def git(*args):
            return subprocess.run(['git', '-C', str(root), *args], check=True,
                                  capture_output=True, text=True, timeout=1).stdout.strip()
        if Path(git('rev-parse', '--show-toplevel')).resolve() == root.resolve():
            result['git_revision'] = git('rev-parse', 'HEAD')
            result['git_dirty'] = bool(git('status', '--porcelain', '--untracked-files=normal'))
    except (OSError, subprocess.SubprocessError):
        pass  # Source digest remains available without Git in deployed releases.
    return result


def config_identity(config, threshold=None):
    values = asdict(config)
    if threshold is not None:
        values['threshold_db'] = threshold
    for key in ('threshold_db', 'startup_timeout', 'data_timeout', 'retry_delay',
                'retry_max_delay', 'stop_timeout', 'alert_hold'):
        values[key] = float(values[key])
    values['excluded_mhz'] = [[float(lo), float(hi)] for lo, hi in values['excluded_mhz']]
    canonical = json.dumps(values, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return dict(id=hashlib.sha256(canonical.encode()).hexdigest(), values=json.loads(canonical))


class Duration:
    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.last = None
        self.maximum = None

    def observe(self, seconds):
        seconds = max(0.0, seconds)
        self.count += 1
        self.total += seconds
        self.last = seconds
        self.maximum = max(self.maximum or 0.0, seconds)

    def snapshot(self):
        return dict(count=self.count, last_ms=None if self.last is None else self.last * 1000,
                    mean_ms=self.total * 1000 / self.count if self.count else None,
                    max_ms=None if self.maximum is None else self.maximum * 1000)


class Rate:
    """Counter samples, not per-row timestamps: <= 12 samples, ~5 s window."""
    def __init__(self, now):
        self.samples = deque([(now, 0)], maxlen=12)

    def snapshot(self, now, count):
        if now - self.samples[-1][0] >= .5:
            self.samples.append((now, count))
        while len(self.samples) > 2 and self.samples[1][0] <= now - 5:
            self.samples.popleft()
        start, previous = self.samples[0]
        elapsed = now - start
        return dict(rows_per_second=(count - previous) / elapsed if elapsed > 0 else 0.0,
                    window_seconds=elapsed)


class ScanCoverage:
    """Estimate scan boundaries from this fork's first output frequency.

    This is observed CSV frequency coverage, NOT USB sample-loss detection.
    Expanded tuning ranges must not overlap; ambiguous configurations are
    explicitly unsupported. Interval memory is bounded even for hostile CSV.
    """
    def __init__(self, ranges_mhz):
        self.ranges = [(lo * 1000000, hi * 1000000) for lo, hi in ranges_mhz]
        expanded = sorted((lo, lo + ((hi - lo + 19) // 20) * 20) for lo, hi in ranges_mhz)
        self.supported = all(a[1] <= b[0] for a, b in zip(expanded, expanded[1:]))
        self.marker = self.ranges[0][0]
        self.started = None
        self.intervals = []
        self.completed = 0
        self.incomplete = 0
        self.invalidated = 0
        self.last = None

    def invalidate(self):
        if self.started is not None:
            self.invalidated += 1
        self.started = None
        self.intervals = []

    def observe(self, measurement, now):
        if not self.supported:
            return
        if measurement.low_hz == self.marker:
            if self.started is not None:
                covered = sum(hi - lo for lo, hi in self.intervals)
                expected = sum(hi - lo for lo, hi in self.ranges)
                complete = covered == expected
                self.completed += int(complete)
                self.incomplete += int(not complete)
                self.last = dict(complete=complete, coverage_fraction=covered / expected,
                                 duration_seconds=max(0, now - self.started),
                                 ended_monotonic=now)
            self.started = now
            self.intervals = []
        if self.started is None:
            return
        additions = [(max(lo, measurement.low_hz), min(hi, measurement.high_hz))
                     for lo, hi in self.ranges
                     if max(lo, measurement.low_hz) < min(hi, measurement.high_hz)]
        merged = []
        for lo, hi in sorted(self.intervals + additions):
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
            else:
                merged.append((lo, hi))
        if len(merged) > 4096:
            self.invalidate()
        else:
            self.intervals = merged

    def snapshot(self):
        return dict(method='csv_start_frequency_boundary', supported=self.supported,
                    unsupported_reason=None if self.supported else 'overlapping_tuning_ranges',
                    synchronized=self.started is not None, complete_scans=self.completed,
                    current_scan_started_monotonic=self.started,
                    incomplete_scans=self.incomplete, invalidated_scans=self.invalidated,
                    last_scan=self.last)

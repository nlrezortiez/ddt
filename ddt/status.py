"""Read-only status CLI: never opens SDR, GPIO or I2C and never starts runtime."""
import argparse
import json
import math
import sys
import time

from .diagnostics import boot_id, process_identity

SCHEMA_VERSION = 2


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def read_status(path):
    with open(path, encoding='utf-8') as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('status exceeds 1 MiB')
    def reject_constant(value):
        raise ValueError(f'non-finite JSON number: {value}')
    status = json.loads(raw, parse_constant=reject_constant)
    if not isinstance(status, dict) or status.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('unsupported status schema (expected version 2)')
    if type(status.get('pid')) is not int or status['pid'] <= 0:
        raise ValueError('invalid runtime PID')
    if type(status.get('process_start_ticks')) is not int or status['process_start_ticks'] < 0:
        raise ValueError('invalid process identity')
    if not isinstance(status.get('boot_id'), str) or not status['boot_id']:
        raise ValueError('invalid boot ID')
    if not number(status.get('updated_monotonic')) or not isinstance(status.get('state'), str):
        raise ValueError('invalid status timestamp/state')
    # Required readiness data is validated even for liveness-only queries.
    if not isinstance(status.get('sdr'), dict) or type(status['sdr'].get('ready')) is not bool:
        raise ValueError('invalid SDR readiness')
    if not number(status['sdr'].get('data_timeout_seconds')) or status['sdr']['data_timeout_seconds'] <= 0:
        raise ValueError('invalid data timeout')
    last = status.get('last_measurement_monotonic')
    if last is not None and not number(last):
        raise ValueError('invalid measurement timestamp')
    for key in ('version', 'configuration', 'metrics', 'hardware', 'timings', 'scan'):
        if not isinstance(status.get(key), dict):
            raise ValueError(f'invalid {key} object')
    for name in ('display', 'gpio'):
        device = status['hardware'].get(name)
        if not isinstance(device, dict) or any(type(device.get(key)) is not bool for key in ('enabled', 'ready')):
            raise ValueError(f'invalid {name} state')
    for name in ('received_rate', 'valid_rate', 'queue'):
        if not isinstance(status['metrics'].get(name), dict):
            raise ValueError(f'invalid {name} metrics')
    scan = status['scan'].get('last_scan')
    if scan is not None and (not isinstance(scan, dict) or type(scan.get('complete')) is not bool):
        raise ValueError('invalid last scan')
    return status


def assess(status, max_age=15.0):
    """Recompute ages on each read; a stored true ready flag cannot stay fresh."""
    if not number(max_age) or max_age <= 0:
        raise ValueError('max-age must be positive and finite')
    now = time.monotonic()
    age = now - status['updated_monotonic']
    reasons = []
    if status['boot_id'] != boot_id():
        reasons.append('different_boot')
    try:
        start_ticks, state = process_identity(status['pid'])
        if start_ticks != status['process_start_ticks'] or state in ('Z', 'X'):
            reasons.append('process_identity_mismatch_or_dead')
    except (OSError, ValueError, IndexError):
        reasons.append('process_not_found')
    if age < 0 or age > max_age:
        reasons.append('stale_status')
    if status['state'] in ('stopping', 'stopped'):
        reasons.append('runtime_stopped')
    alive = not reasons
    last = status.get('last_measurement_monotonic')
    measurement_age = None if last is None else now - last
    fresh = measurement_age is not None and 0 <= measurement_age <= status['sdr']['data_timeout_seconds']
    ready = alive and status.get('mode') == 'live' and status['state'] == 'receiving' and status['sdr']['ready'] and fresh
    if not ready:
        reasons.append('receiver_not_ready' if fresh else 'no_fresh_measurements')
    warnings = []
    hardware_ready = True
    for name in ('display', 'gpio'):
        hardware = status.get('hardware', {}).get(name, {})
        if hardware.get('enabled') and not hardware.get('ready'):
            warnings.append(f'{name}_unavailable')
            hardware_ready = False
    scan = status.get('scan', {})
    if not scan.get('supported'):
        warnings.append('scan_coverage_unsupported')
    elif not scan.get('synchronized'):
        warnings.append('scan_not_synchronized')
    if scan.get('last_scan') and not scan['last_scan']['complete']:
        warnings.append('incomplete_last_scan')
    if status.get('metrics', {}).get('overflow_dropped_rows', 0):
        warnings.append('queue_overflow_recorded')
    return dict(alive=alive, ready=bool(ready), reasons=reasons,
                device_ready=bool(ready and hardware_ready), warnings=warnings,
                status_age_seconds=age, measurement_age_seconds=measurement_age)


def readable(status, assessment):
    def fmt(value, digits=3):
        return 'n/a' if value is None else f'{value:.{digits}f}'
    version = status.get('version', {})
    metrics = status.get('metrics', {})
    queue = metrics.get('queue', {})
    config = status.get('configuration', {})
    lines = [f"Runtime: {'alive' if assessment['alive'] else 'NOT ALIVE'}; SDR: {'READY' if assessment['ready'] else 'NOT READY'}",
             f"State: {status['state']} (mode={status.get('mode', 'unknown')})",
             f"Version: {version.get('git_revision') or 'unknown'} dirty={version.get('git_dirty')}",
             f"Source SHA256: {version.get('source_sha256')}",
             f"Config ID: {config.get('id')} (startup={status.get('startup_config_id')})",
             f"Boot: {status['boot_id']}; instance: {status.get('instance_id')}; PID: {status['pid']}",
             f"Status age: {fmt(assessment['status_age_seconds'])} s; measurement age: {fmt(assessment['measurement_age_seconds'])} s",
             f"First data after: {fmt(status.get('first_measurement_after_seconds'))} s; restarts: {status.get('restarts')}",
             f"Rows: received={metrics.get('received_rows')} valid={status.get('valid_rows')} invalid={status.get('invalid_rows')}",
             f"Loss: overflow={metrics.get('overflow_dropped_rows')} shutdown_discard={metrics.get('shutdown_discarded_rows')} reader_errors={metrics.get('reader_errors')}",
             f"Queue: {queue.get('depth')}/{queue.get('capacity')} high_watermark={queue.get('high_watermark')}",
             f"Rate: received={fmt(metrics.get('received_rate', {}).get('rows_per_second'))}/s valid={fmt(metrics.get('valid_rate', {}).get('rows_per_second'))}/s"]
    for name in ('display', 'gpio'):
        device = status.get('hardware', {}).get(name, {})
        state = 'disabled' if device.get('enabled') is False else ('ready' if device.get('ready') else 'unavailable')
        lines.append(f"{name.upper()}: {state}; errors={device.get('errors')}; last_error={device.get('last_error')}")
    for name, timing in status.get('timings', {}).items():
        lines.append(f"Timing {name}: mean={fmt(timing.get('mean_ms'))} ms max={fmt(timing.get('max_ms'))} ms")
    for name in ('display', 'gpio'):
        timing = status.get('hardware', {}).get(name, {}).get('operation_duration', {})
        lines.append(f"Timing {name}: mean={fmt(timing.get('mean_ms'))} ms max={fmt(timing.get('max_ms'))} ms")
    scan = status.get('scan', {})
    lines.append(f"Observed CSV scan: supported={scan.get('supported')} synchronized={scan.get('synchronized')} complete={scan.get('complete_scans')} incomplete={scan.get('incomplete_scans')} invalidated={scan.get('invalidated_scans')}")
    if scan.get('last_scan'):
        last = scan['last_scan']
        age = time.monotonic() - last['ended_monotonic']
        lines.append(f"Last CSV scan: coverage={fmt(last.get('coverage_fraction'))} duration={fmt(last.get('duration_seconds'))} s age={fmt(age)} s")
    failure = status.get('last_failure')
    lines.append(f"Last failure: {json.dumps(failure, ensure_ascii=False)}")
    transitions = status.get('transitions', [])
    if transitions:
        lines.append(f"Last transition: {json.dumps(transitions[-1], ensure_ascii=False)}")
    lines.append('Assessment: ' + (', '.join(assessment['reasons']) or 'fresh live measurements'))
    lines.append('Warnings: ' + (', '.join(assessment['warnings']) or 'none'))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', default='/run/ddt/status.json')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--max-age', type=float, default=15.0)
    args = parser.parse_args(argv)
    try:
        status = read_status(args.file)
        assessment = assess(status, args.max_age)
        if args.json:
            print(json.dumps(dict(snapshot=status, assessment=assessment), ensure_ascii=False, allow_nan=False))
        else:
            print(readable(status, assessment))
        return 0 if assessment['device_ready'] else 1
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError) as exc:
        if args.json:
            print(json.dumps(dict(error=str(exc), assessment=dict(alive=False, ready=False))))
        else:
            print(f'Status unavailable: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

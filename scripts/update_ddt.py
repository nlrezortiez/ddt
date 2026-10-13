#!/usr/bin/python3
"""Transactional updater for the existing Alpine/OpenRC release installation.

Install this standalone, standard-library-only file as /usr/local/sbin/update_ddt.
It intentionally does not install system packages or migrate service/config files.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time


class UpdateError(RuntimeError):
    pass


def commit_sha(value):
    if not re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', value):
        raise argparse.ArgumentTypeError('use a full Git commit SHA, not a branch/tag or file checksum')
    return value.lower()


@contextlib.contextmanager
def update_lock(path):
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError('another update is running') from exc
        # Keep the inode: unlinking a lock file permits two independent locks.
        yield


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def switch_release(current, target):
    if not current.is_symlink():
        raise UpdateError(f'{current} must be a symlink')
    with tempfile.TemporaryDirectory(prefix='.switch-', dir=current.parent) as directory:
        link = Path(directory) / 'current'
        link.symlink_to(target)
        os.replace(link, current)
        sync_directory(current.parent)


def release_processes(release, proc=Path('/proc')):
    """Include orphan sweep processes, not just the PID named in status.json."""
    result = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            executable = (entry / 'exe').resolve(strict=True)
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
            runtime = any(argv[i:i + 2] == [b'-m', b'ddt'] for i in range(len(argv) - 1))
            if executable == release / 'build/bin/sweep' or (
                    runtime and (entry / 'cwd').resolve(strict=True) == release):
                result.append(int(entry.name))
        except FileNotFoundError:
            pass  # Process exited between /proc reads.
    return result


SNAPSHOT = '''
import json, sys
from pathlib import Path
from ddt.status import read_status, assess
s = read_status(sys.argv[1])
a = assess(s)
if Path('/proc/%s/cwd' % s['pid']).resolve() != Path.cwd():
    raise RuntimeError('wrong runtime directory')
if s['version']['git_revision'] != sys.argv[2] or s['version']['git_dirty'] is not False:
    raise RuntimeError('wrong runtime commit or dirty source')
if not a['device_ready']:
    raise RuntimeError('device not ready: %s' % a)
print(json.dumps({k: s[k] for k in ('instance_id', 'restarts', 'valid_rows')}))
'''


class Updater:
    def __init__(self, revision, *, base=Path('/opt/ddt'), config=Path('/etc/ddt/detector.json'),
                 init=Path('/etc/init.d/sweep'), conf=Path('/etc/conf.d/sweep'),
                 status=Path('/run/ddt/status.json'), source=Path('/root/ddt'), repo=None):
        self.revision, self.base = revision, base
        self.config, self.init, self.conf, self.status = config, init, conf, status
        self.source, self.repo = source, repo
        self.current = base / 'current'
        self.previous = self.current.resolve(strict=True)
        self.release = None
        self.backup = None
        self.log = None
        self.original_files = {}

    def say(self, message):
        print(message, flush=True)
        if self.log:
            self.log.write(message + '\n')
            self.log.flush()

    def run(self, args, *, cwd=None, timeout=900, capture=False, env=None):
        args = [str(arg) for arg in args]
        self.say('Run: ' + ' '.join(args[:2]))
        with subprocess.Popen(args, cwd=cwd, env=env, start_new_session=True,
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE if capture else self.log,
                              stderr=self.log) as process:
            try:
                output, _ = process.communicate(timeout=timeout)
            except BaseException:
                # Do not leave a compiler/pip/test child running after interruption.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
            if process.returncode:
                raise UpdateError(f'{args[0]} failed (exit={process.returncode}); see update.log')
            return output.decode().strip() if capture else None

    def journal(self, phase, **extra):
        write_json(self.backup / 'transaction.json', dict(
            phase=phase, previous=str(self.previous), release=str(self.release) if self.release else None,
            revision=self.revision, **extra))

    def snapshot(self, release, revision):
        return json.loads(self.run(
            [release / '.venv/bin/python3', '-c', SNAPSHOT, self.status, revision],
            cwd=release, timeout=5, capture=True))

    def wait_ready(self, release, revision, timeout=60, stable_seconds=10):
        deadline = time.monotonic() + timeout
        first = None
        last_error = 'no snapshot'
        while time.monotonic() < deadline:
            try:
                current = self.snapshot(release, revision)
                identity = (current['instance_id'], current['restarts'])
                if first is None or first[1] != identity or current['valid_rows'] < first[2]:
                    first = (time.monotonic(), identity, current['valid_rows'])
                if (time.monotonic() - first[0] >= stable_seconds
                        and current['valid_rows'] > first[2]):
                    return
            except (UpdateError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                first = None
                last_error = str(exc)
            time.sleep(1)
        raise UpdateError(f'device did not become stably ready within {timeout}s: {last_error}')

    def check_service_files(self, release):
        for installed, relative in ((self.init, 'deploy/openrc/sweep'),
                                    (self.conf, 'deploy/openrc/sweep.conf')):
            if installed.read_bytes() != (release / relative).read_bytes():
                raise UpdateError(f'{installed} differs from {relative}; manual migration required')
        # Do not silently accept another service layout even if both releases have it.
        expected = ['project_dir="/opt/ddt/current"', 'python="/opt/ddt/current/.venv/bin/python3"']
        settings = [line.strip() for line in self.conf.read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith('#')]
        if settings != expected:
            raise UpdateError('only the standard /opt/ddt/current service layout is supported')

    def preflight(self):
        if not self.current.is_symlink() or self.previous.parent != self.base / 'releases':
            raise UpdateError('expected current symlink pointing directly into /opt/ddt/releases')
        for command in ('git', 'make', 'cmake', 'cc', 'pkg-config', 'rc-service'):
            if not shutil.which(command):
                raise UpdateError(f'missing tool: {command}; install prerequisites manually')
        self.check_service_files(self.previous)
        if shutil.disk_usage(self.base).free < 1024 ** 3:
            raise UpdateError('need at least 1 GiB free before building')
        updates = self.base / 'updates'
        updates.mkdir(exist_ok=True)
        self.backup = Path(tempfile.mkdtemp(prefix=self.revision[:12] + '-', dir=updates))
        self.log = (self.backup / 'update.log').open('a', buffering=1)
        self.say(f'Update log/backup: {self.backup}')
        for path, name in ((self.config, 'detector.json'), (self.init, 'sweep.init'),
                           (self.conf, 'sweep.conf')):
            self.original_files[path] = path.read_bytes()
            shutil.copy2(path, self.backup / name)
        (self.backup / 'previous-release').write_text(str(self.previous) + '\n')
        self.journal('preflight')
        self.previous_revision = self.run(['git', 'rev-parse', 'HEAD'], cwd=self.previous, capture=True)
        self.run(['rc-service', 'sweep', 'status'], timeout=20)
        self.snapshot(self.previous, self.previous_revision)

    def prepare(self):
        repo = self.repo or self.run(['git', '-C', self.source, 'remote', 'get-url', 'origin'], capture=True)
        # A unique final path permits retry after failed builds; never relocate a venv.
        self.release = Path(tempfile.mkdtemp(prefix=self.revision + '-', dir=self.base / 'releases'))
        self.journal('building')
        self.run(['git', 'clone', '--no-hardlinks', '--no-checkout', '--', repo, self.release])
        self.run(['git', 'fetch', 'origin', self.revision], cwd=self.release)
        actual = self.run(['git', 'rev-parse', 'FETCH_HEAD^{commit}'], cwd=self.release, capture=True)
        if actual != self.revision:
            raise UpdateError('fetched object is not the requested commit')
        self.run(['git', 'checkout', '--detach', self.revision], cwd=self.release)
        self.check_service_files(self.release)
        py = self.release / '.venv/bin/python3'
        self.run([sys.executable, '-m', 'venv', self.release / '.venv'])
        self.run([py, '-m', 'pip', 'install', 'pip==25.2', 'setuptools==80.9.0', 'wheel==0.45.1'])
        wheels = self.release / 'dist/wheels'
        wheels.mkdir(parents=True)
        self.run([py, '-m', 'pip', 'wheel', '--no-build-isolation', '--wheel-dir', wheels,
                  '-r', 'requirements.txt'], cwd=self.release, env=dict(os.environ, PYPI='1'))
        self.run([py, '-m', 'pip', 'install', '--no-index', '--find-links', wheels,
                  '-r', 'requirements.txt'], cwd=self.release)
        self.run([py, '-m', 'pip', 'check'], cwd=self.release)
        self.run([py, '-c', 'import lgpio; from luma.oled.device import ssd1306'], cwd=self.release)
        self.run([py, '-c', 'from pathlib import Path; from ddt.config import Config; '
                  'import sys; c = Config.load(sys.argv[1]);\n'
                  'if Path(c.sweep_binary).resolve() != Path("build/bin/sweep").resolve():\n'
                  '    raise ValueError("custom sweep_binary requires manual deployment")',
                  self.config], cwd=self.release)
        self.run(['make', 'build'], cwd=self.release)
        self.run(['make', f'PYTHON={py}', 'check'], cwd=self.release,
                 env=dict(os.environ, DDT_SWEEP=str(self.release / 'build/bin/sweep')))
        self.run(['make', f'PYTHON={py}', 'replay'], cwd=self.release)
        manifest = self.run(['sh', 'scripts/manifest.sh'], cwd=self.release,
                            env=dict(os.environ, PYTHON=str(py)), capture=True)
        (self.backup / 'manifest.txt').write_text(manifest + '\n')
        freeze = self.run([py, '-m', 'pip', 'freeze', '--all'], cwd=self.release, capture=True)
        (self.release / 'dist/python-freeze.txt').write_text(freeze + '\n')
        (self.release / 'dist/wheels.sha256').write_text(''.join(
            f'{hashlib.sha256(wheel.read_bytes()).hexdigest()}  dist/wheels/{wheel.name}\n'
            for wheel in sorted(wheels.glob('*.whl'))))
        # Version/status API is a prerequisite, not something discovered after stop.
        self.run([py, '-c', 'from ddt.status import read_status, assess'], cwd=self.release)
        if self.run(['git', 'status', '--porcelain'], cwd=self.release, capture=True):
            raise UpdateError('build modified tracked/unignored source files')
        self.journal('prepared')

    def stop(self, release):
        self.run(['rc-service', 'sweep', 'stop'], timeout=30)
        deadline = time.monotonic() + 5
        while True:
            remaining = release_processes(release)
            if not remaining:
                return
            if time.monotonic() >= deadline:
                raise UpdateError(f'release still has running processes: {remaining}; manual recovery required')
            time.sleep(0.2)

    def activate(self):
        # Another operator must not alter config/current during the build.
        if not self.current.is_symlink() or self.current.resolve() != self.previous:
            raise UpdateError('current changed during build; refusing activation')
        for path, original in self.original_files.items():
            if path.read_bytes() != original:
                raise UpdateError(f'{path} changed during build; refusing activation')
        self.snapshot(self.previous, self.previous_revision)
        self.journal('stopping_previous')
        try:
            self.stop(self.previous)
            self.journal('switching')
            switch_release(self.current, self.release)
            self.journal('starting')
            self.run(['rc-service', 'sweep', 'start'], timeout=30)
            self.wait_ready(self.release, self.revision)
            self.journal('success')
        except BaseException as original:
            # A second Ctrl+C/HUP must not interrupt bounded rollback commands.
            saved = {sig: signal.signal(sig, signal.SIG_IGN)
                     for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
            try:
                self.say(f'Activation failed: {original}. Rolling back to {self.previous}')
                self.journal('rolling_back', error=str(original))
                active = self.current.resolve(strict=True)
                if active not in (self.previous, self.release):
                    raise UpdateError('unexpected current target; refusing to overwrite it')
                self.stop(active)
                switch_release(self.current, self.previous)
                self.run(['rc-service', 'sweep', 'start'], timeout=30)
                self.wait_ready(self.previous, self.previous_revision)
                self.journal('rolled_back', error=str(original))
            except BaseException as rollback_error:
                self.say(f'ROLLBACK FAILED: {rollback_error}. Manual recovery required; backup={self.backup}')
                self.journal('rollback_failed', error=str(original), rollback_error=str(rollback_error))
                raise UpdateError('update and rollback failed; inspect transaction.json and service') from rollback_error
            finally:
                for sig, handler in saved.items():
                    signal.signal(sig, handler)
            raise UpdateError('update failed; previous release restored and verified') from original


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('revision', type=commit_sha)
    parser.add_argument('--repo', help='trusted Git remote URL/path; default: origin URL from /root/ddt')
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error('run as root on the Raspberry Pi')
    previous_umask = os.umask(0o027)

    def interrupted(signum, frame):
        raise UpdateError(f'interrupted by signal {signum}')

    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    for sig, handler in saved.items():
        # Respect nohup; restoring a HUP handler here would defeat its protection.
        if sig != signal.SIGHUP or handler != signal.SIG_IGN:
            signal.signal(sig, interrupted)
    updater = None
    try:
        with update_lock(Path('/opt/ddt/update.lock')):
            updater = Updater(args.revision, repo=args.repo)
            updater.preflight()
            if updater.previous_revision == args.revision:
                updater.journal('already_current')
                updater.say('Requested commit is already running and ready; no changes.')
                return 0
            updater.prepare()
            updater.activate()
            updater.say(f'Update successful: {updater.release}\nPrevious release retained: {updater.previous}')
            return 0
    except (UpdateError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f'Update failed: {exc}', file=sys.stderr)
        if updater and updater.backup:
            try:
                state = json.loads((updater.backup / 'transaction.json').read_text())
                if state['phase'] in ('preflight', 'building', 'prepared'):
                    updater.journal('preparation_failed', error=str(exc))
            except (OSError, ValueError, KeyError):
                pass  # Preserve the original failure even if storage itself failed.
            print(f'Log and recovery state: {updater.backup}', file=sys.stderr)
        return 1
    finally:
        if updater and updater.log:
            updater.log.close()
        for sig, handler in saved.items():
            signal.signal(sig, handler)
        os.umask(previous_umask)


if __name__ == '__main__':
    raise SystemExit(main())

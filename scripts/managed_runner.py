"""Serve exactly one PHP build job, then retire the disposable Linux host."""
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time

from managed_profiles import PROFILES, TARGET, identify, labels, validate
from runner_pool import child_environment, download_runner, start_runner, stop

# Avoid keeping an unused VM when another runner wins the queued job.
ACCEPT_SECONDS = 10 * 60
MAX_SECONDS = 340 * 60


def validate_namespace_shape(cpu_count, memory_bytes):
    # MemTotal excludes the kernel's reserved memory.
    if cpu_count != 4 or not 7_000_000_000 <= memory_bytes <= 8 * 1024 ** 3:
        raise ValueError('Expected a Namespace 4-vCPU/8-GB Linux host')


def serve_job(directory, env, log, accept_seconds=ACCEPT_SECONDS, max_seconds=MAX_SECONDS):
    started = time.monotonic()
    process = None
    try:
        previous = set((directory / '_diag').glob('Worker_*.log'))
        process = start_runner(directory, None, env, log)
        assigned = False
        while True:
            result = process.poll()
            assigned = assigned or bool(set((directory / '_diag').glob('Worker_*.log')) - previous)
            if result is not None:
                if result != 0 or not assigned:
                    raise RuntimeError('Runner listener exited without completing a job')
                print('Completed one job; retiring host.', flush=True)
                return 1
            elapsed = time.monotonic() - started
            if not assigned and elapsed >= accept_seconds:
                print('No job assigned within ten minutes; retiring unused host.', flush=True)
                return 0
            if elapsed >= max_seconds:
                raise TimeoutError('Host execution deadline reached')
            time.sleep(2)
    finally:
        stop(process)


def configure_runner(directory, key, name, token, env, log):
    result = subprocess.run([
        './config.sh', '--unattended', '--ephemeral', '--url', f'https://github.com/{TARGET}',
        '--token', token, '--name', name, '--work', '_work',
        '--no-default-labels', '--labels', ','.join(labels(key)),
    ], cwd=directory, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    log.write(result.stdout)
    log.flush()
    if result.returncode:
        diagnostic = result.stdout.decode(errors='replace').replace(token, '[redacted]')
        diagnostic = re.sub(r'(?i)(authorization:|bearer)\s+\S+', r'\1 [redacted]', diagnostic)
        print(f'Runner configuration exited with code {result.returncode}.', flush=True)
        for line in diagnostic.splitlines()[-12:]:
            print(line, flush=True)
        raise RuntimeError('Runner configuration failed')


def serve():
    os.umask(0o077)
    token = os.environ.pop('RUNNER_REGISTRATION_TOKEN')
    name = os.environ.pop('POOL')
    identity = identify(name)
    if identity is None:
        raise ValueError('Invalid managed runner name')
    key = identity
    profile = PROFILES[key]
    if os.environ.pop('POOL_ARCH', profile['arch']) != profile['arch']:
        raise ValueError('Runner name does not match the selected architecture')
    if not re.fullmatch(r'[A-Za-z0-9_]+', token):
        raise ValueError('Invalid registration token')
    system = platform.system()
    version = platform.freedesktop_os_release()['VERSION_ID'] if system == 'Linux' else platform.mac_ver()[0]
    validate(key, system, platform.machine(), version)
    memory = int(re.search(r'^MemTotal:\s+(\d+) kB$', Path('/proc/meminfo').read_text(), re.M)[1]) * 1024
    if profile['provider'] == 'namespace':
        validate_namespace_shape(os.cpu_count(), memory)
    print(f'Host verified: Ubuntu {version}, {platform.machine()}, '
          f'{os.cpu_count()} vCPUs, {memory} bytes RAM.', flush=True)
    env = child_environment(os.environ)
    # Older PHP installers expect the native distro ID in GitHub-runner mode.
    env['DIST_ID'] = platform.freedesktop_os_release()['ID']
    directory = Path(tempfile.mkdtemp(prefix='managed-runner-', dir=os.environ['RUNNER_TEMP']))

    def interrupted(signum, frame):
        raise InterruptedError('Host workflow interrupted')

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        download_runner(directory, profile['arch'], profile['platform'])
        print(f'Pinned {key} runner verified.', flush=True)
        with (directory / 'host.log').open('wb') as log:
            configure_runner(directory, key, name, token, env, log)
            token = ''
            print(f'Registered ephemeral {key} runner for one job.', flush=True)
            serve_job(directory, env, log)
    finally:
        subprocess.run(['sudo', 'rm', '-rf', '--', str(directory)], check=False)


if __name__ == '__main__':
    try:
        serve()
    except BaseException as error:
        print(f'::error::Managed runner failed ({type(error).__name__}).', file=sys.stderr)
        sys.exit(1)

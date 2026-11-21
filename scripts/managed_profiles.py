"""Native Linux hosts for the PHP builder's temporary, single-job pool."""
import re

from runner_pool import validate_host

TARGET = 'shivammathur/php-builder'
HOST = 'setup-php/php-builder'
BRANCH = 'feature/php-build-runner-pool'
LIMITS = {'namespace': 160, 'github': 60}
PROFILES = {}
for provider in LIMITS:
    for version in ('22.04', '24.04'):
        for arch, ns_arch in [('x64', 'amd64'), ('arm64', 'arm64')]:
            key = f'{provider}-u{version[:2]}-{arch}'
            label = f'ubuntu-{version}' + ('-arm' if arch == 'arm64' else '')
            aliases = [label]
            if version == '24.04' and arch == 'x64':
                aliases.append('ubuntu-latest')
            PROFILES[key] = {
                'prefix': f'runner-pool-php-{key}-',
                'provider': provider, 'arch': arch, 'platform': f'ubuntu-{version}',
                'image': f'nscloud-ubuntu-{version}-{ns_arch}-1x2' if provider == 'namespace' else label,
                'labels': aliases,
            }


def identify(name):
    for key, profile in PROFILES.items():
        prefixes = [profile['prefix'], profile['prefix'].replace('runner-pool-php-', 'runner-pool-phpbuild-')]
        for prefix in prefixes:
            if re.fullmatch(prefix + r'[0-9]{8}-[0-9]{6}-[a-f0-9]{8}', name):
                return key
    return None


def labels(key):
    profile = PROFILES[key]
    return ['self-hosted', 'Linux', 'X64' if profile['arch'] == 'x64' else 'ARM64', *profile['labels']]


def validate(key, system, machine, version):
    profile = PROFILES[key]
    validate_host(profile['platform'], profile['arch'], system, machine, version)

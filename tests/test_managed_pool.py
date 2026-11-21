import collections
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from managed_pool import capacity_reservations, matching_profiles, plan_launches
from managed_profiles import LIMITS, PROFILES, identify, labels, validate
from managed_runner import validate_namespace_shape


def job(ident, label='ubuntu-24.04', **extra):
    return {'id': ident, 'labels': [label], 'status': 'queued', **extra}


def name(key, number):
    return PROFILES[key]['prefix'] + f'20261121-080000-{number:08x}'


class ManagedPoolTests(unittest.TestCase):
    def test_stopped_hosts_release_capacity_but_keep_busy_cleanup_guard(self):
        stopped, live, unknown = [name('namespace-u24-x64', i) for i in range(3)]
        protected = {stopped, live, unknown}
        runners = [{'name': stopped, 'status': 'offline', 'busy': True},
                   {'name': live, 'status': 'online', 'busy': True},
                   {'name': unknown, 'status': 'offline', 'busy': True}]
        reservations = capacity_reservations(protected, runners, {
            stopped: {'status': 'completed'}, live: {'status': 'in_progress'}})
        self.assertEqual(reservations, {live, unknown})
        self.assertIn(stopped, protected)
        planned, _, _, _ = plan_launches(reservations, runners, [job(1)], {'namespace': 3, 'github': 0})
        self.assertEqual(planned, ['namespace-u24-x64'])

    def test_every_provider_name_fits_github_limit_and_old_hosts_remain_owned(self):
        for key in PROFILES:
            current = name(key, 1)
            self.assertLessEqual(len(current), 64)
            self.assertEqual(identify(current), key)
            self.assertEqual(identify(current.replace('runner-pool-php-', 'runner-pool-phpbuild-')), key)

    def test_shared_provider_limits_and_both_architectures(self):
        jobs = [job(i) for i in range(300)] + [job(1000 + i, 'ubuntu-24.04-arm') for i in range(300)]
        planned, _, _, _ = plan_launches(set(), [], jobs, LIMITS)
        self.assertEqual(collections.Counter(PROFILES[key]['provider'] for key in planned), LIMITS)
        self.assertEqual(collections.Counter(PROFILES[key]['arch'] for key in planned), {'x64': 70, 'arm64': 70})
        self.assertTrue(all(PROFILES[key]['image'].endswith('-2x4') for key in planned if key.startswith('namespace-')))

    def test_busy_hosts_use_capacity_and_idle_hosts_cover_one_job(self):
        key = 'namespace-u24-x64'
        busy, idle = name(key, 1), name(key, 2)
        runners = [{'name': busy, 'status': 'online', 'busy': True},
                   {'name': idle, 'status': 'online', 'busy': False}]
        jobs = [job(1, status='in_progress', runner_name=busy), job(2), job(3), job(4)]
        planned, _, covered, deferred = plan_launches({busy, idle}, runners, jobs, {'namespace': 2, 'github': 1})
        self.assertEqual(planned, ['github-u24-x64'])
        self.assertEqual(sum(covered.values()), 1)
        self.assertEqual(sum(deferred.values()), 1)

    def test_native_baselines_and_architectures_cannot_mix(self):
        candidates = matching_profiles(job(1, 'ubuntu-22.04-arm'))
        self.assertEqual(candidates, ['namespace-u22-arm64', 'github-u22-arm64'])
        self.assertEqual(matching_profiles(job(2, 'Linux')), [])
        self.assertEqual(matching_profiles(job(3, 'macos-15')), [])
        for key in candidates:
            self.assertNotIn('ubuntu-24.04-arm', labels(key))
            validate(key, 'Linux', 'aarch64', '22.04')
            with self.assertRaises(ValueError):
                validate(key, 'Linux', 'aarch64', '24.04')

    def test_booting_hosts_reserve_capacity_without_duplicate_launches(self):
        protected = {name('namespace-u24-x64', i) for i in range(80)}
        planned, _, covered, _ = plan_launches(protected, [], [job(i) for i in range(80)], LIMITS)
        self.assertEqual(planned, [])
        self.assertEqual(sum(covered.values()), 80)
        self.assertTrue(all(identify(value) == 'namespace-u24-x64' for value in protected))

    def test_minimum_namespace_shape_is_enforced(self):
        validate_namespace_shape(2, 3_900_000_000)
        for cpu, memory in [(1, 2_000_000_000), (2, 8_000_000_000), (2, 2_000_000_000)]:
            with self.assertRaises(ValueError):
                validate_namespace_shape(cpu, memory)


if __name__ == '__main__':
    unittest.main()

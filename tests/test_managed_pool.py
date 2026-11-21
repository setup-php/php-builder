import collections
import sys
import subprocess
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from managed_pool import capacity_reservations, gh, launch_order, matching_profiles, pages, plan_launches, refresh_registration, target_jobs
from managed_profiles import LIMITS, PROFILES, identify, labels, validate
from managed_runner import validate_namespace_shape


def job(ident, label='ubuntu-24.04', **extra):
    return {'id': ident, 'labels': [label], 'status': 'queued', **extra}


def name(key, number):
    return PROFILES[key]['prefix'] + f'20261121-080000-{number:08x}'


class ManagedPoolTests(unittest.TestCase):
    def test_fresh_target_inventory_is_reused_but_wrong_run_is_refetched(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.dict('os.environ', {'TARGET_RUN_IDS': '1', 'TARGET_JOB_CACHE_DIR': root}), \
                patch('managed_pool.pages', return_value=[job(3, run_id=1)]) as api:
            cached = Path(root) / '1-jobs.json'
            cached.write_text(json.dumps([job(2, run_id=1)]))
            self.assertEqual(target_jobs(), [job(2, run_id=1)])
            api.assert_not_called()
            cached.write_text(json.dumps([job(2, run_id=9)]))
            self.assertEqual(target_jobs(), [job(3, run_id=1)])
            self.assertEqual(api.call_count, 1)
            cached.write_text(json.dumps([job(2, run_id=1)]))
            with patch('managed_pool.time.time', return_value=cached.stat().st_mtime + 91):
                self.assertEqual(target_jobs(), [job(3, run_id=1)])
            self.assertEqual(api.call_count, 2)

    def test_installation_tokens_follow_the_repository_for_reads_and_writes(self):
        success = subprocess.CompletedProcess([], 0, b'{}', b'')
        with patch.dict('os.environ', {'GH_TARGET_TOKEN': 'target-token', 'GH_HOST_TOKEN': 'host-token'}), \
                patch('managed_pool.subprocess.run', return_value=success) as command:
            for repo, token in [('shivammathur/php-builder', 'target-token'), ('setup-php/php-builder', 'host-token')]:
                for method in ['GET', 'POST', 'PUT', 'DELETE']:
                    gh(f'repos/{repo}/actions/runners', method)
                    self.assertEqual(command.call_args.kwargs['env']['GH_TOKEN'], token)
            gh('repos/setup-php/php-builder-other/actions/runs')
            self.assertIsNone(command.call_args.kwargs['env'])

    def test_registration_secret_uses_host_token_and_stdin(self):
        with patch.dict('os.environ', {'GH_HOST_TOKEN': 'host-token'}), \
                patch('managed_pool.gh', return_value={'token': 'registration-secret'}), \
                patch('managed_pool.subprocess.run') as command:
            refresh_registration('test-environment')
            self.assertEqual(command.call_args.kwargs['env']['GH_TOKEN'], 'host-token')
            self.assertEqual(command.call_args.kwargs['input'], b'registration-secret')
            self.assertNotIn('registration-secret', command.call_args.args[0])

    def test_api_headers_record_rate_reset_and_stop_further_writes(self):
        response = subprocess.CompletedProcess([], 1,
            b'HTTP/2.0 403 Forbidden\r\nX-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 2000000000\r\n\r\n{"message":"rate limit exceeded"}', b'HTTP 403')
        with tempfile.TemporaryDirectory() as root, \
                patch.dict('os.environ', {'GH_RATE_LIMIT_STATE': root + '/rate.json'}), \
                patch('managed_pool.subprocess.run', return_value=response) as command:
            with self.assertRaisesRegex(RuntimeError, 'quota exhausted'):
                gh('repos/example/php-builder/environments/test', 'PUT', {})
            self.assertEqual(json.loads(Path(root + '/rate.json').read_text())['resume_after'], 2000000010)
            with self.assertRaisesRegex(RuntimeError, 'recorded reset'):
                gh('repos/example/php-builder/environments/test', 'DELETE')
            self.assertEqual(command.call_count, 1)
        with patch('managed_pool.subprocess.run', return_value=subprocess.CompletedProcess(
                [], 0, b'HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n{"ok":true}', b'')):
            self.assertEqual(gh('repos/example/php-builder'), {'ok': True})

    def test_read_token_is_scoped_to_workflow_reads_and_can_fall_back(self):
        success = subprocess.CompletedProcess([], 0, b'{}', b'')
        denied = subprocess.CompletedProcess([], 1, b'', b'HTTP 403')
        with patch.dict('os.environ', {'GH_TARGET_READ_TOKEN': 'test-read-token'}), \
                patch('managed_pool.subprocess.run', return_value=success) as command:
            gh('repos/shivammathur/php-builder/actions/runs/1/jobs')
            self.assertEqual(command.call_args.kwargs['env']['GH_TOKEN'], 'test-read-token')
            gh('repos/shivammathur/php-builder/actions/runners')
            self.assertIsNone(command.call_args.kwargs['env'])
            gh('repos/shivammathur/php-builder/actions/runs/1/rerun', 'POST')
            self.assertIsNone(command.call_args.kwargs['env'])
        with patch.dict('os.environ', {'GH_TARGET_READ_TOKEN': 'test-read-token'}), \
                patch('managed_pool.subprocess.run', side_effect=[denied, success]) as command:
            self.assertEqual(gh('repos/shivammathur/php-builder/actions/runs/1/jobs'), {})
            self.assertIsNone(command.call_args.kwargs['env'])

    def test_slow_api_reads_are_bounded_and_leave_other_runs_available(self):
        with patch('managed_pool.subprocess.run', side_effect=subprocess.TimeoutExpired('gh', 15)) as command:
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                gh('repos/example/php-builder/actions/runs/1/jobs')
            self.assertEqual(command.call_args.kwargs['timeout'], 15)

    def test_job_inventory_uses_smaller_pages_until_complete(self):
        with patch('managed_pool.gh', side_effect=[{'jobs': list(range(50))}, {'jobs': [50]}]) as api:
            self.assertEqual(pages('jobs?filter=latest', 'jobs'), list(range(51)))
        self.assertEqual([call.args[0] for call in api.call_args_list],
                         ['jobs?filter=latest&per_page=50&page=1', 'jobs?filter=latest&per_page=50&page=2'])

    def test_unavailable_run_does_not_block_other_queued_work(self):
        def read(route, key):
            if '/runs/1/' in route:
                raise RuntimeError('temporary API failure')
            return [job(2)]
        with patch.dict('os.environ', {'TARGET_RUN_IDS': '1 2'}), \
                patch('managed_pool.pages', side_effect=read), patch('builtins.print'):
            self.assertEqual(target_jobs(), [job(2)])

    def test_both_providers_start_replenishing_without_waiting_for_each_other(self):
        planned = ['namespace-u24-x64'] * 80 + ['github-u24-arm64'] * 60
        ordered = launch_order(planned)
        self.assertEqual(collections.Counter(ordered), collections.Counter(planned))
        self.assertEqual(ordered[:4], ['namespace-u24-x64', 'github-u24-arm64'] * 2)
        self.assertEqual(launch_order([]), [])

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
        self.assertEqual(collections.Counter(PROFILES[key]['arch'] for key in planned), {'x64': 50, 'arm64': 50})
        self.assertTrue(all(PROFILES[key]['image'].endswith('-4x8') for key in planned if key.startswith('namespace-')))

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
        validate_namespace_shape(4, 7_900_000_000)
        for cpu, memory in [(2, 4_000_000_000), (4, 4_000_000_000), (2, 8_000_000_000)]:
            with self.assertRaises(ValueError):
                validate_namespace_shape(cpu, memory)


if __name__ == '__main__':
    unittest.main()

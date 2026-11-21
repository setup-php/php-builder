"""Maintain bounded, single-job Linux pools for explicitly selected PHP builds."""
import collections
import concurrent.futures
import datetime
import json
import os
import secrets
import subprocess

from managed_profiles import PROFILES, TARGET, HOST, BRANCH, LIMITS, identify, labels



def gh(path, method='GET', data=None):
    command = ['gh', 'api', path, '--method', method]
    if data is not None:
        command += ['--input', '-']
    response = subprocess.run(command, input=json.dumps(data).encode() if data is not None else None,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if response.returncode:
        raise RuntimeError('GitHub API operation failed: ' + method + ' ' + path.split('/actions/')[0])
    return json.loads(response.stdout) if response.stdout else None


class InventoryTruncated(RuntimeError):
    pass


def pages(path, key):
    result = []
    for page in range(1, 100):
        response = gh(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
        if key == 'workflow_runs' and response.get('total_count', 0) >= 1000:
            raise InventoryTruncated('Workflow search requires smaller time windows')
        result.extend(response[key])
        if len(response[key]) < 100:
            return result
    raise RuntimeError('Inventory exceeded pagination limit')


def history(start, end):
    def stamp(value):return value.strftime('%Y-%m-%dT%H:%M:%SZ')
    route=f'repos/{HOST}/actions/runs?branch={BRANCH}&created={stamp(start)}..{stamp(end)}'
    try:
        return pages(route,'workflow_runs')
    except InventoryTruncated:
        seconds=int((end-start).total_seconds())
        if seconds < 1:raise
        middle=start+datetime.timedelta(seconds=seconds//2)
        return history(start,middle)+history(middle+datetime.timedelta(seconds=1),end)


def host_runs(now):
    now=now.replace(microsecond=0)
    runs={run['id']:run for run in history(now-datetime.timedelta(days=1),now)}
    # Active runs can predate the history window. Never retire their resources
    # just because a filtered history was truncated or its window moved on.
    for status in ('queued','in_progress','waiting','pending','requested'):
        for run in pages(f'repos/{HOST}/actions/runs?branch={BRANCH}&status={status}','workflow_runs'):
            runs[run['id']]=run
    return list(runs.values())


def pool_runs(runs):
    return {name: run for run in runs
            if run['display_title'].startswith('Managed runner ')
            and identify(name := run['display_title'].removeprefix('Managed runner '))}


def active_pools(runs):
    return {name: run for name, run in pool_runs(runs).items() if run['status'] != 'completed'}


def target_jobs():
    identifiers = [int(value) for value in os.environ['TARGET_RUN_IDS'].split()]
    if not identifiers or len(identifiers) != len(set(identifiers)):
        raise ValueError('Explicit, unique target run IDs are required')
    def read(ident):
        return pages(f'repos/{TARGET}/actions/runs/{ident}/jobs?filter=latest', 'jobs')
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        return [job for group in executor.map(read, identifiers) for job in group]


def matching_profiles(job):
    requested = {label.lower() for label in job.get('labels', [])}
    # Only route explicitly requested Ubuntu images, never generic Linux jobs.
    if not any(label.startswith('ubuntu-') for label in requested):
        return []
    return [key for key in PROFILES if requested <= {label.lower() for label in labels(key)}]


def plan_launches(protected, runners, jobs, limits):
    registered = {runner['name']: runner for runner in runners}
    consumed = {job.get('runner_name') for job in jobs if job['status'] != 'queued'}
    occupied = collections.Counter(PROFILES[identify(name)]['provider'] for name in protected)
    budgets = {provider: max(0, limit - occupied[provider]) for provider, limit in limits.items()}
    waiting = []
    for name in sorted(protected):
        if name not in consumed and not registered.get(name, {}).get('busy', False):
            waiting.append({label.lower() for label in labels(identify(name))})
    for runner in runners:
        if not identify(runner['name']) and runner['status'] == 'online' and not runner['busy']:
            waiting.append({label['name'].lower() for label in runner['labels']})
    queued, covered, deferred, planned = collections.Counter(), collections.Counter(), collections.Counter(), []
    unique = {job['id']: job for job in jobs}
    # Round-robin the requested images so ARM64 jobs get capacity alongside x64.
    groups = collections.defaultdict(collections.deque)
    for job in sorted(unique.values(), key=lambda item: item['id']):
        if job['status'] == 'queued' and not job.get('runner_name') and matching_profiles(job):
            groups[tuple(sorted(job['labels']))].append(job)
    while any(groups.values()):
        for group in groups.values():
            if not group:
                continue
            job = group.popleft()
            candidates = matching_profiles(job)
            key = candidates[0]
            queued[key] += 1
            requested = {label.lower() for label in job['labels']}
            match = next((index for index, available in enumerate(waiting) if requested <= available), None)
            if match is not None:
                waiting.pop(match)
                covered[key] += 1
                continue
            chosen = next((candidate for candidate in candidates if budgets[PROFILES[candidate]['provider']]), None)
            if chosen is None:
                deferred[key] += 1
                continue
            planned.append(chosen)
            budgets[PROFILES[chosen]['provider']] -= 1
    return planned, dict(queued), dict(covered), dict(deferred)


def create_environment(name):
    gh(f'repos/{HOST}/environments/{name}', 'PUT', {
        'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True},
    })
    gh(f'repos/{HOST}/environments/{name}/deployment-branch-policies', 'POST',
       {'name': BRANCH, 'type': 'branch'})


def refresh_registration(name):
    registration = gh(f'repos/{TARGET}/actions/runners/registration-token', 'POST')
    subprocess.run(['gh', 'secret', 'set', 'RUNNER_REGISTRATION_TOKEN', '-R', HOST, '--env', name],
                   input=registration['token'].encode(), stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True)
    del registration


def launch(now, key):
    profile = PROFILES[key]
    pool = profile['prefix'] + now.strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(4)
    create_environment(pool)
    refresh_registration(pool)
    gh(f'repos/{HOST}/actions/workflows/managed-runner.yml/dispatches', 'POST',
       {'ref': BRANCH, 'inputs': {'pool': pool, 'image': profile['image']}})
    print(f'Dispatched {key}: {pool}', flush=True)


def maintain():
    limits = {provider: int(os.environ.get(provider.upper() + '_CAPACITY', maximum))
              for provider, maximum in LIMITS.items()}
    if any(not 0 <= limits[provider] <= maximum for provider, maximum in LIMITS.items()):
        raise ValueError('Provider capacity exceeds the authorized limit')
    dry_run = os.environ.get('DRY_RUN', 'false').lower() == 'true'
    now = datetime.datetime.now(datetime.timezone.utc)
    runs = host_runs(now)
    known = pool_runs(runs)
    active = active_pools(runs)
    runners = pages(f'repos/{TARGET}/actions/runners', 'runners')
    registered = {runner['name'] for runner in runners}
    environments = pages(f'repos/{HOST}/environments', 'environments')
    jobs = target_jobs()
    protected = set(active)
    for runner in runners:
        name = runner['name']
        if identify(name) is None or name in active:
            continue
        finished = name in known and known[name]['status'] == 'completed'
        if runner['busy'] or (runner['status'] != 'offline' and not finished):
            protected.add(name)
            continue
        if not dry_run:
            gh(f'repos/{TARGET}/actions/runners/{runner["id"]}', 'DELETE')
            print(f'Removed stopped runner {name}', flush=True)
    for environment in environments:
        name = environment['name']
        if identify(name) is None or name in protected:
            continue
        age = now - datetime.datetime.fromisoformat(environment['created_at'].replace('Z', '+00:00'))
        if name not in known and age < datetime.timedelta(minutes=30):
            # Reserve a slot for dispatch visibility delays or partial setup.
            protected.add(name)
            continue
        # A completed single-job host must release its slot immediately, even
        # if its environment was created less than thirty minutes ago.
        if not dry_run:
            gh(f'repos/{HOST}/environments/{name}', 'DELETE')
            print(f'Removed finished environment {name}', flush=True)
    consumed = {job.get('runner_name') for job in jobs if job['status'] != 'queued'}
    for name, run in active.items():
        age = now - datetime.datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
        if name not in registered and name not in consumed and age > datetime.timedelta(minutes=45):
            if not dry_run:
                gh(f"repos/{HOST}/actions/runs/{run['id']}/cancel", 'POST')
                print(f'Cancelled stale queued host {name}', flush=True)
    planned, queued, covered, deferred = plan_launches(protected, runners, jobs, limits)
    print(json.dumps({'queued_jobs': queued, 'covered_by_existing_hosts': covered,
                      'active_hosts': len(active), 'reserved_hosts': len(protected),
                      'provider_capacities': limits, 'launching': dict(collections.Counter(planned)),
                      'capacity_deferred': deferred, 'dry_run': dry_run}), flush=True)
    if dry_run or not planned:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = [executor.submit(launch, now, key) for key in planned]
        errors = []
        for result in results:
            try:
                result.result()
            except Exception as error:
                errors.append(type(error).__name__)
        if errors:
            raise RuntimeError('Runner launch failures: ' + ', '.join(errors))


if __name__ == '__main__':
    maintain()

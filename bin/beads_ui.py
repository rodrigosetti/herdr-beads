#!/usr/bin/env python3
"""Dashboard discovery and bounded v1 requests; never launch a service or close/remove resources.

Use project --folder ABS [--config ABS] or request --json JSON [--config ABS].
Discovery reads only canonical identity, owner-only service health and public status.
The service health API enumerates current attempts (including retained closed Beads),
not superseded attempt IDs; this bridge never opens the private lifecycle store.
"""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from beads_contract import Refusal, validate
from beads_identity import command, configure_executables, executable, machine_id, repository_identity
from beads_service import load_config, private_json
from beads_transport import TransportError, error_response, read_input, request as transport_request

UI_METHODS = frozenset(('status', 'start_or_focus', 'resume', 'handoff', 'release', 'reconcile'))


def default_config_path():
    state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
    if not state.is_absolute():
        raise Refusal('invalid_request', 'XDG_STATE_HOME must be absolute.')
    return state.resolve() / 'herdr-beads/service.json'


def failure(error, request_id=None):
    return error_response(getattr(error, 'code', 'io_error'), str(error), request_id,
                          refused=isinstance(error, Refusal) or getattr(error, 'refused', False),
                          retryable=getattr(error, 'retryable', isinstance(error, OSError)))


def configured_target(repository):
    # Exit 1 means unconfigured, not a provider/auth failure. Never infer a remote
    # or invoke gh; even a repository with no master can be inspected safely.
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    try:
        result = subprocess.run([executable('git'), 'config', '--get', 'herdr.beads.target'],
                                cwd=repository['root'], env=env, text=True,
                                capture_output=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refusal('evidence_unavailable', f'Cannot read configured lifecycle target: {error}', True) from error
    if result.returncode == 1:
        return {'mode': 'local-target', 'repository': repository, 'ref': 'refs/heads/master'}
    if result.returncode:
        raise Refusal('evidence_unavailable', f'Cannot read configured lifecycle target: {result.stderr.strip()[:500]}', True)
    try:
        target = json.loads(result.stdout)
    except ValueError as error:
        raise Refusal('invalid_record', 'git config herdr.beads.target must contain a JSON Target.') from error
    validate('run', target, 'Target')
    if target['mode'] != 'github-pr' and target['repository'] != repository:
        raise Refusal('repository_mismatch', 'Configured lifecycle target must identify the canonical repository.')
    return target


def service_health(config, config_path):
    health = private_json(Path(config_path).parent / 'service-status.json')
    if not isinstance(health, dict) or health.get('schemaVersion') != 1:
        raise Refusal('invalid_record', 'Unsupported lifecycle service health.')
    if health.get('state') != 'running' or health.get('socketPath') != config['socketPath']:
        raise Refusal('evidence_unavailable', 'Lifecycle service is not running on its configured socket.', True)
    pid = health.get('pid')
    if type(pid) is not int or pid <= 0:
        raise Refusal('invalid_record', 'Lifecycle service health has no valid PID.')
    try:
        os.kill(pid, 0)
    except OSError as error:
        raise Refusal('evidence_unavailable', 'Lifecycle service process is unavailable; no local fallback is permitted.', True) from error
    validate('controller', health.get('support'), 'Support')
    if not isinstance(health.get('runs'), list) or not isinstance(health.get('projects'), list):
        raise Refusal('invalid_record', 'Lifecycle service health must enumerate runs and projects.')
    return health


def project(folder, config_path=None):
    folder = Path(folder)
    if not folder.is_absolute():
        raise Refusal('invalid_request', 'project --folder requires an absolute local path.')
    config = None
    config_error = None
    try:
        config_path = Path(config_path) if config_path is not None else default_config_path()
        config = load_config(config_path)
        configure_executables(config['executables'])
    except (Refusal, OSError, ValueError, TypeError, KeyError) as error:
        config_error = failure(error)['error']
    repository = repository_identity(str(folder))
    root = repository['root']
    env = os.environ.copy()
    env.pop('BEADS_DIR', None)
    if (Path(root) / '.beads').is_dir():
        env['BEADS_DIR'] = str(Path(root) / '.beads')
    location = json.loads(command(['bd', '--sandbox', 'where', '--json'], root, env=env, timeout=20))
    if not isinstance(location, dict) or not isinstance(location.get('path'), str) or not Path(location['path']).is_absolute():
        raise Refusal('database_mismatch', 'bd where did not resolve an absolute canonical database.')
    scope = {'repository': repository, 'beadsDir': str(Path(location['path']).resolve()), 'machineId': machine_id()}
    result = {'project': {'folder': root, 'beads': scope['beadsDir'], 'machine': None, 'scope': scope},
              'target': None, 'service': {'available': False, 'support': None, 'error': config_error},
              'runs': [], 'epics': [], 'errors': []}
    try:
        result['target'] = configured_target(repository)
    except (Refusal, OSError, ValueError, TypeError, KeyError) as error:
        result['errors'].append(failure(error)['error'])
    if config is None:
        return result
    try:
        health = service_health(config, config_path)
        result['service'].update(available=True, support=health['support'], error=None)
        for row in health['projects']:
            if isinstance(row, dict) and row.get('repository') == repository and row.get('beadsDir') == scope['beadsDir']:
                result['epics'].extend(row.get('epics', []))
                result['errors'].extend(row.get('errors', []))
        seen = set()
        for row in health['runs']:
            if not isinstance(row, dict) or not isinstance(row.get('scope'), dict):
                continue
            run_scope = row['scope']
            if any(run_scope.get(key) != value for key, value in scope.items()):
                continue
            validate('run', run_scope, 'Scope')
            validate('run', row.get('fence'), 'Fence')
            identity = (run_scope['issueId'], row['fence']['runId'])
            if identity in seen:
                continue
            seen.add(identity)
            value = {'apiVersion': 1, 'requestId': str(uuid.uuid4()), 'method': 'status',
                     'scope': run_scope, 'params': {'runId': row['fence']['runId']}}
            response = transport_request(value, config['socketPath'])
            if response['outcome'] == 'error':
                result['service'].update(available=False, error=response['error'])
                result['runs'] = []
                return result
            validate('controller', response, 'Response')
            run = response.get('run')
            if run and (run['scope'] != run_scope or run['fence']['runId'] != identity[1]):
                raise Refusal('invalid_record', 'Controller status returned a different run scope/identity.')
            result['runs'].append(response)
            if response.get('error'):
                result['errors'].append({'scope': run_scope, **response['error']})
        return result
    except (Refusal, TransportError, OSError, ValueError, TypeError, KeyError) as error:
        result['service'].update(available=False, error=failure(error)['error'])
        result['runs'] = []
        return result


def request_json(text, config_path=None):
    request_id = None
    try:
        value = read_input(io.StringIO(text), time.monotonic() + 5)
        request_id = value.get('requestId')
        if not isinstance(request_id, str) or not request_id:
            request_id = None
        if value.get('method') not in UI_METHODS:
            raise Refusal('invalid_request', 'Dashboard methods are limited to status/start_or_focus/resume/handoff/release/reconcile; no closure, receipt, observation or cleanup execution.')
        if value.get('apiVersion') != 1:
            raise Refusal('unsupported_version', 'Use apiVersion 1.')
        validate('controller', value, 'Request')
        if value['scope']['machineId'] != machine_id():
            raise Refusal('remote_machine', 'Dashboard lifecycle requests operate on the recorded local machine only; no local fallback.')
        config = load_config(Path(config_path) if config_path is not None else default_config_path())
        return transport_request(value, config['socketPath'])
    except (Refusal, TransportError, OSError, ValueError, TypeError, KeyError) as error:
        return failure(error, request_id)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal('invalid_request', message)


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        commands = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
        discover = commands.add_parser('project')
        discover.add_argument('--folder', required=True)
        discover.add_argument('--config')
        forward = commands.add_parser('request')
        forward.add_argument('--json', required=True)
        forward.add_argument('--config')
        args = parser.parse_args(argv)
        result = project(args.folder, args.config) if args.command == 'project' else request_json(args.json, args.config)
    except (Refusal, TransportError, OSError, ValueError, TypeError, KeyError) as error:
        result = failure(error)
    try:
        output = json.dumps(result, ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        result = error_response('io_error', 'Lifecycle service returned unencodable JSON.')
        output = json.dumps(result)
    print(output)
    return 1 if result.get('outcome') in ('error', 'refused') else 0


if __name__ == '__main__':
    raise SystemExit(main())

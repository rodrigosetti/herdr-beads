#!/usr/bin/env python3
"""Per-user lifecycle scheduling. No shell commands, closure, or resource removal."""
import argparse
import copy
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time
import traceback
import uuid

from beads_contract import Refusal, now, validate
from beads_controller import Controller
from beads_epics import observe_epics
from beads_identity import Authority, canonical_path, configure_executables, machine_id
from beads_store import Store
from beads_transport import ControllerServer, TransportError

CONFIG_KEYS = {'schemaVersion', 'pollSeconds', 'socketPath', 'storeRoot', 'executables', 'projects'}
TOOLS = {'git', 'bd', 'gh', 'omp', 'hc', 'herdr-app'}


def private_json(path):
    """Reject aliases, non-owner records, and unbounded configuration/status input."""
    path = canonical_path(str(path), 'invalid_record')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 1024 * 1024):
            raise Refusal('invalid_record', f'Expected bounded owner-only JSON: {path}')
        with os.fdopen(fd, closefd=False) as stream:
            return json.load(stream)
    finally:
        os.close(fd)


def load_config(path):
    config = private_json(path)
    if not isinstance(config, dict) or set(config) != CONFIG_KEYS or config['schemaVersion'] != 1:
        raise Refusal('invalid_record', 'Unsupported lifecycle service configuration.')
    interval = config['pollSeconds']
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 5 <= interval <= 300:
        raise Refusal('invalid_record', 'pollSeconds must be between 5 and 300 (default 30).')
    for name in ('socketPath', 'storeRoot'):
        canonical_path(config[name], 'invalid_record')
    tools = config['executables']
    if not isinstance(tools, dict) or set(tools) - TOOLS or not all(tools.get(k) for k in ('git', 'bd')):
        raise Refusal('invalid_record', 'Configure explicit git/bd and only supported executables.')
    for name, value in tools.items():
        if value is not None and (not isinstance(value, str) or not Path(value).is_absolute() or '\x00' in value):
            raise Refusal('invalid_record', f'Executable {name} must be an absolute path or null.')
    if not isinstance(config['projects'], list):
        raise Refusal('invalid_record', 'projects must be a list of explicit canonical projects.')
    for project in config['projects']:
        if not isinstance(project, dict) or set(project) != {'repository', 'beadsDir'}:
            raise Refusal('invalid_record', 'Invalid project registration.')
        validate('run', {**project, 'machineId': 'local', 'issueId': 'service-project'}, 'Scope')
        for value in (*project['repository'].values(), project['beadsDir']):
            canonical_path(value, 'invalid_record')
    return config


def write_health(path, value):
    directory = canonical_path(str(path.parent), 'invalid_record')
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refusal('invalid_record', 'Service health directory must be owner-only.')
    temporary = directory / ('.service-status-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=True, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Project:
    bd_error_type = Refusal

    def __init__(self, authority, scope):
        self.authority, self.scope = authority, scope

    def bd(self, *args):
        # Defense in depth: the background epic observer gets reads only.
        if not args or args[0] not in ('list', 'show'):
            raise Refusal('unsupported_capability', 'Background epic observation is read-only.')
        return self.authority.bd(self.scope, *args)


class LifecycleServer(ControllerServer):
    def __init__(self, controller, config, health_path):
        super().__init__(controller, config['socketPath'])
        self.config = config
        self.health_path = Path(health_path)
        self.health = {'schemaVersion': 1, 'state': 'starting', 'pid': os.getpid(),
                       'startedAt': now(), 'lastReconcileAt': None, 'pollSeconds': config['pollSeconds'],
                       'socketPath': config['socketPath'], 'storeRoot': config['storeRoot'],
                       'support': controller.support(), 'projects': [], 'runs': [], 'errors': []}

    def _projects(self):
        projects = {json.dumps(p, sort_keys=True): p for p in self.config['projects']}
        for scope in self.controller.store.documents():
            if scope['machineId'] == self.controller.machine_id:
                project = {k: scope[k] for k in ('repository', 'beadsDir')}
                projects[json.dumps(project, sort_keys=True)] = project
        result = []
        for project in projects.values():
            scope = {**project, 'issueId': 'service-project', 'machineId': self.controller.machine_id}
            row = copy.deepcopy(project)
            try:
                self.controller.authority.validate_scope(scope, self.controller.machine_id)
                row.update(observe_epics(Project(self.controller.authority, scope)))
            except (Refusal, OSError, ValueError) as error:
                row['errors'] = [{'error': str(error)}]
            result.append(row)
        return result

    def reconcile_once(self):
        # Bounded by actual progress, not session/process lifetime. Uncertain
        # effects remain journaled and recover by observation on the next wake.
        while not self.stopped.is_set() and self.controller.work_once():
            pass
        errors = self.controller.reconcile_runs()
        while not self.stopped.is_set() and self.controller.work_once():
            pass
        runs = []
        for scope in self.controller.store.documents():
            if scope['machineId'] != self.controller.machine_id:
                errors.append({'code': 'remote_machine', 'scope': scope,
                               'message': 'Retained remote-machine run is not executable here.'})
                continue
            doc = self.controller.store.read(scope)
            run = doc['runs'].get(doc['current'])
            if run:
                operation = self.controller.latest(doc, run)
                runs.append({k: copy.deepcopy(run[k]) for k in (
                    'scope', 'fence', 'owner', 'disposition', 'target', 'submissions',
                    'coverage', 'closure', 'cleanup', 'issueObservation')})
                visible = [op['public'] for op in doc['operations'].values()
                           if op['public']['fence']['runId'] == run['fence']['runId']
                           and op['public']['state'] in ('pending', 'running')]
                if operation and operation not in visible:
                    visible.append(operation)
                for operation in visible:
                    if operation.get('error'):
                        errors.append({'scope': scope, 'operationId': operation['operationId'], **operation['error']})
        self.health.update(state='running', lastReconcileAt=now(), runs=runs,
                           projects=self._projects(), errors=errors, support=self.controller.support())
        write_health(self.health_path, self.health)

    def _work(self):
        try:
            # First cycle always observes durable state, even without a window,
            # event, omp process, or network. Coalesce events, never trust them as proof.
            while not self.stopped.is_set():
                self.wakeup.clear()
                self.reconcile_once()
                self.wakeup.wait(self.config['pollSeconds'])
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            self.failure = TransportError(getattr(error, 'code', 'dependency_failed'), str(error))
            self.health.update(state='failed', errors=[{'code': self.failure.code, 'message': str(error)}])
            self.stopped.set()
        finally:
            if self.failure is None:
                self.health['state'] = 'stopped'
            try:
                write_health(self.health_path, self.health)
            except Exception:
                traceback.print_exc(file=sys.stderr)


def adapters(config):
    """Only the repository's concrete omp adapter may supply execution authority."""
    try:
        import beads_omp
    except ModuleNotFoundError as error:
        if error.name != 'beads_omp':
            raise
        return None, None
    factory = getattr(beads_omp, 'create_adapters', None)
    if factory is None:
        return None, None
    execution, workspace = factory(config['executables'])
    execution.socket_path = config['socketPath']
    return execution, workspace


def serve(config, health_path):
    configure_executables(config['executables'])
    execution, workspace = adapters(config)
    controller = Controller(machine_id(), Store(config['storeRoot']), execution=execution, workspace=workspace)
    server = LifecycleServer(controller, config, health_path)

    def stop(_number, _frame):
        server.stopped.set()
        server.wakeup.set()

    def wake(_number, _frame):
        server.wakeup.set()

    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, stop)
    for number in (signal.SIGUSR1, signal.SIGCONT):
        signal.signal(number, wake)
    server.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=('serve', 'status'))
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        health_path = Path(args.config).parent / 'service-status.json'
        if args.command == 'serve':
            serve(config, health_path)
            return 0
        health = private_json(health_path)
        live = health.get('state') == 'running'
        try:
            pid = health['pid']
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                raise ValueError('Invalid service PID')
            os.kill(pid, 0)
        except (OSError, KeyError, ValueError):
            live = False
        health['processObservedAlive'] = live
        print(json.dumps(health, ensure_ascii=True, allow_nan=False))
        return 0 if live else 1
    except (Refusal, TransportError, OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({'state': 'failed', 'code': getattr(error, 'code', 'io_error'),
                          'message': str(error), 'config': args.config}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

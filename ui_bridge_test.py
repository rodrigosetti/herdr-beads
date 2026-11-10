"""Dashboard discovery uses canonical Git identities and the public local socket only."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
import uuid

import controller_test
from controller_test import git
from beads_controller import Controller
import beads_identity
from beads_service import write_health
from beads_transport import ControllerServer
import beads_ui


class UIBridgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = controller_test.LifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = self.fixture.home
        self.state = self.home / 'xdg'
        machine_path = self.state / 'go-bd/machine-id'
        machine_path.parent.mkdir(parents=True)
        self.machine = str(uuid.uuid4())
        machine_path.write_text(self.machine + '\n')
        self.fixture.scope['machineId'] = self.machine
        self.fixture.controller = Controller(self.machine, self.fixture.store, self.fixture.authority,
                                             self.fixture.execution, self.fixture.workspace)
        self.beads = self.fixture.repo / '.beads'
        self.beads.mkdir()
        self.bd_log = self.home / 'bd-calls.jsonl'
        self.bd = self.home / 'bd'
        self.bd.write_text(f'''#!{sys.executable}
import json, os, sys
with open({str(self.bd_log)!r}, 'a') as stream:
    stream.write(json.dumps({{'argv':sys.argv[1:], 'beads':os.environ.get('BEADS_DIR'), 'cwd':os.getcwd()}}) + '\\n')
if sys.argv[1:] != ['--sandbox', 'where', '--json']:
    sys.exit(91)
print(json.dumps({{'path':{str(self.beads)!r}}}))
''')
        self.bd.chmod(0o700)
        self.config_path = self.state / 'herdr-beads/service.json'
        self.config_path.parent.mkdir(parents=True, mode=0o700)
        self.config = {'schemaVersion': 1, 'pollSeconds': 30,
                       'socketPath': str(self.home / 'c.sock'),
                       'storeRoot': str(self.fixture.store.root),
                       'executables': {'git': shutil.which('git'), 'bd': str(self.bd)}, 'projects': []}
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)
        self.health_path = self.config_path.parent / 'service-status.json'
        self.environment = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.state), 'BEADS_DIR': '/foreign/database'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        identity = patch.object(beads_identity, '_executables', None)
        identity.start()
        self.addCleanup(identity.stop)
        self.servers = []
        self.addCleanup(self.stop_servers)

    def stop_servers(self):
        for server, thread, errors in self.servers:
            server.stopped.set()
            server.wakeup.set()
            thread.join(6)
            server.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def start_server(self):
        server = ControllerServer(self.fixture.controller, self.config['socketPath']).start()
        errors = []
        def serve():
            try:
                server.serve_forever()
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.servers.append((server, thread, errors))

    def health(self, runs=None, **changes):
        value = {'schemaVersion': 1, 'state': 'running', 'pid': os.getpid(),
                 'socketPath': self.config['socketPath'], 'support': self.fixture.controller.support(),
                 'projects': [], 'runs': runs or [], 'errors': []}
        value.update(changes)
        write_health(self.health_path, value)

    def discover(self, folder=None):
        return beads_ui.project(str(folder or self.fixture.repo), self.config_path)

    def test_linked_checkout_uses_canonical_database_not_inherited_context(self):
        linked = self.home / 'linked'
        git(self.fixture.repo, 'worktree', 'add', '-b', 'linked', str(linked))
        subdirectory = linked / 'nested'
        subdirectory.mkdir()
        self.health()
        result = self.discover(subdirectory)
        self.assertEqual(result['project'], {'folder': str(self.fixture.repo), 'beads': str(self.beads),
            'machine': None, 'scope': {key: value for key, value in self.fixture.scope.items() if key != 'issueId'}})
        self.assertEqual(result['target'], self.fixture.target)
        calls = [json.loads(row) for row in self.bd_log.read_text().splitlines()]
        self.assertEqual(calls, [{'argv': ['--sandbox', 'where', '--json'],
                                 'beads': str(self.beads), 'cwd': str(self.fixture.repo)}])
        self.assertTrue(result['service']['available'])
        self.assertEqual(result['runs'], [])
        self.assertFalse((self.fixture.repo / '.git/config').read_text().find('[remote ') >= 0)

    def test_health_filters_full_identity_and_fetches_retained_closed_run(self):
        self.fixture.start()
        self.fixture.authority.row['status'] = 'closed'
        self.fixture.reconcile()
        run = self.fixture.status()['run']
        self.assertEqual(run['closure']['state'], 'observed_closed')
        foreign = []
        for key, replacement in (('machineId', 'remote'), ('beadsDir', '/foreign/.beads'),
                                 ('repository', {'root': '/foreign', 'commonDir': '/foreign/.git'})):
            row = copy.deepcopy(run)
            row['scope'][key] = replacement
            foreign.append(row)
        common = copy.deepcopy(run)
        common['scope']['repository']['commonDir'] = '/foreign/.git'
        self.health(runs=[*foreign, common, run, run])
        self.start_server()
        with patch.object(beads_ui, 'transport_request', wraps=beads_ui.transport_request) as transport:
            result = self.discover()
        self.assertTrue(result['service']['available'], result)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.args[0]['params'], {'runId': run['fence']['runId']})
        self.assertEqual(len(result['runs']), 1)
        response = result['runs'][0]
        self.assertEqual(response['run'], run)
        self.assertEqual(response['run']['closure']['state'], 'observed_closed')
        self.assertIn('manual_cleanup_required', response['run']['cleanup']['blockers'])
        self.assertIn('actions', response)
        self.assertNotIn('"private":', json.dumps(response))
        self.assertTrue(self.fixture.workspace.path.is_dir())

    def test_missing_config_keeps_project_identity_and_explicit_unavailability(self):
        self.config_path.unlink()
        with patch.dict(os.environ, {'PATH': str(self.home) + os.pathsep + os.environ['PATH']}):
            result = self.discover()
        self.assertEqual(result['project']['scope']['machineId'], self.machine)
        self.assertFalse(result['service']['available'])
        self.assertIsNotNone(result['service']['error'])
        self.assertEqual(result['runs'], [])
        self.assertEqual(result['target']['mode'], 'local-target')
        self.assertFalse(Path(self.config['socketPath']).exists())

    def test_dead_health_process_does_not_present_stale_run_as_current(self):
        self.fixture.start()
        self.health(runs=[self.fixture.status()['run']])
        real_kill = os.kill
        def dead(pid, signal):
            if pid == os.getpid() and signal == 0:
                raise ProcessLookupError('dead service')
            return real_kill(pid, signal)
        with patch.object(beads_ui.os, 'kill', side_effect=dead), patch.object(beads_ui, 'transport_request') as transport:
            result = self.discover()
        self.assertFalse(result['service']['available'])
        self.assertEqual(result['runs'], [])
        self.assertIn('no local fallback', result['service']['error']['message'])
        transport.assert_not_called()

    def test_owner_only_health_and_config_are_required(self):
        self.health()
        self.health_path.chmod(0o644)
        result = self.discover()
        self.assertFalse(result['service']['available'])
        self.assertEqual(result['service']['error']['code'], 'invalid_record')
        self.config_path.chmod(0o644)
        response = beads_ui.request_json(json.dumps(self.fixture.request('status', {'runId': None})), self.config_path)
        self.assertEqual(response['error']['code'], 'invalid_record')

    def test_configured_target_is_explicit_and_bad_json_never_falls_back(self):
        target = {'mode': 'github-pr', 'repository': {'host': 'github.com', 'repositoryId': 'id',
                  'owner': 'owner', 'name': 'repo'}, 'ref': 'refs/heads/review'}
        git(self.fixture.repo, 'config', 'herdr.beads.target', json.dumps(target))
        self.health()
        self.assertEqual(self.discover()['target'], target)
        git(self.fixture.repo, 'config', 'herdr.beads.target', '{bad')
        result = self.discover()
        self.assertIsNone(result['target'])
        self.assertEqual(result['errors'][0]['code'], 'invalid_record')
        self.assertTrue(result['service']['available'])

    def test_missing_default_master_is_discoverable_without_remote_inference(self):
        git(self.fixture.repo, 'branch', '-m', 'different')
        self.health()
        self.assertEqual(self.discover()['target']['ref'], 'refs/heads/master')
        self.assertEqual(self.fixture.execution.launches, 0)

    def test_forbidden_methods_never_reach_service_and_retain_request_id(self):
        for method in ('confirm_integration', 'observe', 'close', 'closure', 'cleanup', 'remove'):
            with self.subTest(method=method), patch.object(beads_ui, 'transport_request') as transport:
                value = self.fixture.request(method, {}, request_id='retained-request')
                response = beads_ui.request_json(json.dumps(value), self.config_path)
                self.assertEqual(response['requestId'], 'retained-request')
                self.assertEqual(response['outcome'], 'refused')
                self.assertEqual(response['error']['code'], 'invalid_request')
                transport.assert_not_called()
        self.assertEqual(self.fixture.authority.row['status'], 'open')
        self.assertFalse(self.fixture.workspace.path.exists())

    def test_remote_machine_refused_before_any_local_service_request(self):
        value = self.fixture.request('status', {'runId': None}, request_id='remote-request')
        value['scope']['machineId'] = str(uuid.uuid4())
        with patch.object(beads_ui, 'transport_request') as transport:
            response = beads_ui.request_json(json.dumps(value), self.config_path)
        self.assertEqual(response['requestId'], 'remote-request')
        self.assertEqual(response['error']['code'], 'remote_machine')
        transport.assert_not_called()

    def test_invalid_ui_request_cannot_mutate_policy_or_reach_service(self):
        value = self.fixture.start_request()
        value['params']['policy']['closeAfterIntegration'] = True
        with patch.object(beads_ui, 'transport_request') as transport:
            response = beads_ui.request_json(json.dumps(value), self.config_path)
        self.assertEqual(response['requestId'], value['requestId'])
        self.assertEqual(response['error']['code'], 'invalid_request')
        transport.assert_not_called()
        self.assertEqual(value['params']['policy']['closeAfterIntegration'], True)

    def test_cli_uses_argv_and_default_config_without_stdin_or_launch(self):
        self.health()
        self.start_server()
        cli = str(Path(__file__).parent / 'bin/beads_ui.py')
        result = subprocess.run([sys.executable, cli, 'project', '--folder', str(self.fixture.repo)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['service']['available'])
        value = self.fixture.request('status', {'runId': None}, request_id='argv-request')
        result = subprocess.run([sys.executable, cli, 'request', '--json', json.dumps(value)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=8)
        response = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, response)
        self.assertEqual(response['requestId'], 'argv-request')
        self.assertEqual(response['actions'], [{'method': 'start_or_focus', 'enabled': True, 'refusal': None}])
        self.assertEqual(self.fixture.execution.launches, 0)

    def test_parse_errors_and_unavailable_socket_are_structured(self):
        for text in ('{bad', '[]', '{"method":"status","method":"close"}'):
            response = beads_ui.request_json(text, self.config_path)
            self.assertEqual(response['error']['code'], 'invalid_request')
        value = self.fixture.request('status', {'runId': None}, request_id='offline-request')
        response = beads_ui.request_json(json.dumps(value), self.config_path)
        self.assertEqual(response['requestId'], 'offline-request')
        self.assertEqual(response['error']['code'], 'service_unavailable')
        self.assertFalse(Path(self.config['socketPath']).exists())


if __name__ == '__main__':
    unittest.main()

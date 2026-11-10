"""Lifecycle scheduling uses real Git workspaces/sockets, with seeded execution receipts."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import unittest
from unittest.mock import patch

import controller_test
from controller_test import git
from beads_controller import Controller
from beads_epics import observe_epics
import beads_identity
from beads_service import LifecycleServer, Project, load_config
from beads_transport import request, TransportError
from omp_adapter_test import MemoryHerdr


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = controller_test.LifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.start()
        self.identity = self.fixture.handoff()
        self.controller = Controller('machine', self.fixture.store, self.fixture.authority)
        self.config = {'schemaVersion': 1, 'pollSeconds': 30,
                       'socketPath': str(self.fixture.home / 'c.sock'),
                       'storeRoot': str(self.fixture.store.root),
                       'executables': {'git': shutil.which('git'), 'bd': shutil.which('bd'), 'gh': None},
                       'projects': []}
        self.health_path = self.fixture.home / 'service-status.json'
        self.servers = []
        self.addCleanup(self.stop_servers)

    def stop_servers(self):
        for server, thread, errors in self.servers:
            server.stopped.set()
            server.wakeup.set()
            thread.join(8)
            server.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def start_server(self, poll=30):
        server = LifecycleServer(self.controller, {**self.config, 'pollSeconds': poll}, self.health_path).start()
        errors = []
        def serve():
            try:
                server.serve_forever()
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.servers.append((server, thread, errors))
        return server

    def current(self):
        doc = self.fixture.store.read(self.fixture.scope)
        return doc['runs'][doc['current']]

    def wait_for(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.02)
        self.fail('Service did not reach expected state')

    def integrate(self):
        git(self.fixture.repo, 'merge', '--ff-only', self.identity['head'])

    def test_startup_verifies_offline_without_execution_or_workspace_provider(self):
        self.integrate()
        with patch.object(beads_identity, '_executables', {'git': shutil.which('git'), 'gh': None}):
            server = LifecycleServer(self.controller, self.config, self.health_path)
            server.reconcile_once()
        run = self.current()
        self.assertEqual(run['closure']['reason'], 'Merged — awaiting manual closure')
        self.assertEqual(run['closure']['state'], 'awaiting_manual')
        self.assertIn('BEADS_DIR=', run['closure']['manualCommand'])
        self.assertEqual(run['submissions'][0]['evidence']['targetCommit'], self.identity['head'])
        self.assertIn('manual_cleanup_required', run['cleanup']['blockers'])
        self.assertEqual(self.fixture.authority.row['status'], 'in_progress')
        self.assertTrue(self.fixture.workspace.path.is_dir())

    def test_polling_invalidates_target_history_without_ui_or_event(self):
        self.integrate()
        initial = git(self.fixture.repo, 'rev-parse', self.identity['head'] + '^')
        self.start_server(poll=0.1)
        self.wait_for(lambda: self.current()['closure']['state'] == 'awaiting_manual')
        git(self.fixture.repo, 'update-ref', 'refs/heads/master', initial)
        self.wait_for(lambda: self.current()['closure']['manualCommand'] is None)
        self.assertNotEqual(self.current()['coverage']['state'], 'integrated')
        self.assertTrue(self.fixture.workspace.path.is_dir())

    def test_validated_mutation_wakes_worker_without_poll_deadline(self):
        self.integrate()
        self.start_server()
        self.wait_for(lambda: self.current()['closure']['state'] == 'awaiting_manual')
        git(self.fixture.repo, 'update-ref', 'refs/heads/master', self.identity['head'] + '^')
        run = self.current()
        value = self.fixture.request('reconcile', {'expected': {'fence': run['fence'], 'revision': run['revision']}})
        response = request(value, self.config['socketPath'])
        self.assertEqual(response['outcome'], 'accepted', response)
        self.wait_for(lambda: self.current()['closure']['manualCommand'] is None)

    def test_restart_observes_manual_closure_and_release_without_deletion(self):
        self.fixture.call('release', {'expected': self.fixture.condition(), 'reason': 'owner reviewed'})
        self.fixture.drain()
        self.fixture.authority.row['status'] = 'closed'
        self.integrate()
        server = self.start_server()
        self.wait_for(lambda: self.current()['closure']['state'] == 'observed_closed')
        server.stopped.set()
        server.wakeup.set()
        self.servers[0][1].join(8)
        self.start_server()
        self.wait_for(lambda: json.loads(self.health_path.read_text())['state'] == 'running')
        run = self.current()
        self.assertEqual(run['release']['state'], 'released')
        self.assertEqual(run['closure']['state'], 'observed_closed')
        self.assertIsNone(run['closure']['manualCommand'])
        self.assertIn('manual_cleanup_required', run['cleanup']['blockers'])
        self.assertTrue(self.fixture.workspace.path.is_dir())
        self.assertIn(str(self.fixture.workspace.path), git(self.fixture.repo, 'worktree', 'list', '--porcelain'))

    def test_singleton_refuses_second_service_without_mutating_run(self):
        self.start_server()
        self.wait_for(lambda: self.health_path.exists())
        with self.assertRaises(TransportError) as error:
            LifecycleServer(self.controller, self.config, self.health_path).start()
        self.assertEqual(error.exception.code, 'pending_operation')
        self.assertEqual(self.fixture.execution.launches, 1)

    def test_provider_failure_holds_pr_but_not_independent_local_run(self):
        other = controller_test.LifecycleTests()
        other.setUp()
        self.addCleanup(other.doCleanups)
        other.store = self.fixture.store
        other.controller = other.restart()
        other.controller.store = self.fixture.store
        other.start()
        target = {'mode': 'github-pr', 'repository': {'host': 'github.com', 'repositoryId': 'id',
                  'owner': 'owner', 'name': 'repo'}, 'ref': 'refs/heads/master'}
        other.handoff(target=target)
        authorities = {self.fixture.scope['issueId'] + self.fixture.scope['repository']['root']: self.fixture.authority,
                       other.scope['issueId'] + other.scope['repository']['root']: other.authority}
        class RoutedAuthority:
            def validate_scope(self, scope, machine):
                if scope['issueId'] == 'service-project':
                    raise beads_identity.Refusal('unsupported_capability', 'epic fixture not configured')
                return authorities[scope['issueId'] + scope['repository']['root']].validate_scope(scope, machine)
            def issue(self, scope):
                return authorities[scope['issueId'] + scope['repository']['root']].issue(scope)
        self.controller.authority = RoutedAuthority()
        self.integrate()
        with patch.object(beads_identity, '_executables', {'git': shutil.which('git'), 'gh': None}):
            LifecycleServer(self.controller, self.config, self.health_path).reconcile_once()
        self.assertEqual(self.current()['closure']['state'], 'awaiting_manual')
        doc = self.fixture.store.read(other.scope)
        pr = doc['runs'][doc['current']]
        self.assertEqual(pr['target']['mode'], 'github-pr')
        self.assertEqual(pr['coverage']['state'], 'unknown')
        self.assertIsNone(pr['closure']['manualCommand'])
        self.assertIn('gh', pr['closure']['reason'])

    def test_pending_uncertain_execution_does_not_block_merge_bookkeeping(self):
        run = self.current()
        value = self.fixture.start_request()
        value['params']['expected'] = {'fence': run['fence'], 'revision': run['revision']}
        result = self.fixture.controller.handle(value)
        self.assertEqual(result['outcome'], 'accepted', result)
        with self.fixture.store.transaction(self.fixture.scope) as doc:
            doc['operations'][result['operation']['operationId']]['intent']['started'] = True
        class UnavailableExecution:
            capabilities = []
            def recover(self, *args):
                return None
        self.controller.execution = UnavailableExecution()
        self.controller.workspace = self.fixture.workspace
        # An uncertain execution effect must remain pending without preventing
        # independent canonical target observation.
        self.integrate()
        LifecycleServer(self.controller, self.config, self.health_path).reconcile_once()
        self.assertEqual(self.current()['closure']['state'], 'awaiting_manual')
        self.assertEqual(self.fixture.execution.launches, 1)
        pending = self.fixture.store.read(self.fixture.scope)['operations'][result['operation']['operationId']]
        self.assertEqual(pending['public']['state'], 'running')

    def test_repeated_reassignment_does_not_rotate_generation_each_poll(self):
        self.fixture.authority.row['assignee'] = 'new-owner'
        server = LifecycleServer(self.controller, self.config, self.health_path)
        server.reconcile_once()
        fence = self.current()['fence']
        server.reconcile_once()
        self.assertEqual(self.current()['fence'], fence)
        self.assertEqual(self.current()['disposition'], 'reassigned')
        self.assertIsNone(self.current()['closure']['manualCommand'])

    def test_unchanged_poll_revalidates_without_rewriting_durable_history(self):
        self.integrate()
        server = LifecycleServer(self.controller, self.config, self.health_path)
        server.reconcile_once()
        record = next(self.fixture.store.root.glob('*.json'))
        before = record.read_bytes()
        server.reconcile_once()
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(self.current()['closure']['state'], 'awaiting_manual')

    def test_observation_racing_new_submission_cannot_restore_old_proof(self):
        self.integrate()
        observer = self.controller.integration
        fixture = self.fixture
        class RacingObserver:
            def observe(self, run, receipts):
                result = observer.observe(run, receipts)
                fixture.handoff(revision=2)
                return result
        self.controller.integration = RacingObserver()
        LifecycleServer(self.controller, self.config, self.health_path).reconcile_once()
        run = self.current()
        self.assertEqual(run['coverage']['required'][0]['submissionRevision'], 2)
        self.assertIsNone(run['closure']['manualCommand'])
        self.assertNotEqual(run['coverage']['state'], 'integrated')

    def test_config_rejects_unsafe_paths_and_remote_execution_configuration(self):
        path = self.fixture.home / 'service.json'
        path.write_text(json.dumps(self.config))
        path.chmod(0o600)
        self.assertEqual(load_config(path)['pollSeconds'], 30)
        path.chmod(0o644)
        with self.assertRaises(beads_identity.Refusal):
            load_config(path)
        path.chmod(0o600)
        invalid = {**self.config, 'executables': {**self.config['executables'], 'shell': '/bin/sh'}}
        path.write_text(json.dumps(invalid))
        with self.assertRaises(beads_identity.Refusal):
            load_config(path)


class EpicObservationTests(unittest.TestCase):
    def test_eligible_epic_is_reported_without_closure_or_reopen(self):
        class ReadOnlyAuthority:
            def bd(self, scope, *args):
                if args[:3] == ('list', '--type', 'epic'):
                    return [{'id': 'project-epic', 'status': 'open'}]
                if args[:3] == ('list', '--parent', 'project-epic'):
                    return [{'id': 'project-child', 'status': 'closed', 'parent': 'project-epic'}]
                raise AssertionError(f'Unexpected mutating command: {args}')
        result = observe_epics(Project(ReadOnlyAuthority(), {}))
        self.assertEqual(result['epics'][0]['status'], 'open')
        self.assertTrue(result['epics'][0]['eligible'])
        self.assertEqual(result['epics'][0]['closed_children'], 1)
        self.assertEqual(result['errors'], [])

class SpaceNamePollingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = controller_test.LifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.controller = self.fixture.controller
        self.authority = self.fixture.authority
        self.authority.row['title'] = 'Original title'
        validate_scope = self.authority.validate_scope

        def validate_project(scope, machine):
            if scope['issueId'] == 'service-project':
                scope = {**scope, 'issueId': self.fixture.scope['issueId']}
            validate_scope(scope, machine)

        def bd(scope, *args):
            if args[:3] == ('list', '--type', 'epic'):
                return []
            if args == ('show', self.fixture.scope['issueId']):
                return [copy.deepcopy(self.authority.row)]
            raise AssertionError(f'Unexpected background Beads command: {args}')

        self.authority.validate_scope = validate_project
        self.authority.bd = bd
        self.project = {key: self.fixture.scope[key] for key in ('repository', 'beadsDir')}
        self.config = {'schemaVersion': 1, 'pollSeconds': 0.05,
                       'socketPath': str(self.fixture.home / 'names.sock'),
                       'storeRoot': str(self.fixture.store.root),
                       'executables': {'git': shutil.which('git'), 'bd': shutil.which('bd'),
                                       'hc': '/configured/hc', 'herdr-app': '/configured/herdr-app'},
                       'projects': [self.project]}
        self.health_path = self.fixture.home / 'names-health.json'
        path = str(self.fixture.workspace.path)
        self.hosting = MemoryHerdr([
            {'id': 'retained-space', 'name': 'collision-1', 'folder': path,
             'tabs': [{'id': 'retained-tab', 'terminals': [{'terminal': 't1'}]}]},
            {'id': 'remote-space', 'name': 'Remote title', 'folder': path, 'machine': 'foreign'},
            {'id': 'unrelated-space', 'name': 'Other title', 'folder': str(self.fixture.repo)},
        ], {'t1': {'address': 't1', 'cwd': path, 'state': 'exited',
                   'labels': {'beads.project': self.project['beadsDir'], 'beads.issue': 'collision-1'}}})
        self.commands = []

        def hosting_command(argv, cwd, *, timeout):
            self.commands.append(copy.deepcopy(argv))
            names = {'/configured/hc': 'hc', '/configured/herdr-app': 'herdr-app'}
            normalized = [names[argv[0]], *argv[1:]]
            if normalized[:2] == ['herdr-app', 'do']:
                value = self.hosting.app(normalized[2], **json.loads(normalized[3]))
            else:
                value = self.hosting.call(normalized)
            return json.dumps(value)

        patcher = patch('beads_herdr.command', side_effect=hosting_command)
        patcher.start()
        self.addCleanup(patcher.stop)

    def legacy_session(self):
        self.fixture.workspace.path.mkdir()
        transcript = self.fixture.home / 'retained-session.jsonl'
        transcript.write_text(json.dumps({'type': 'session', 'id': 'exact-session',
                                          'cwd': str(self.fixture.workspace.path)}) + '\n')
        self.authority.row['metadata']['omp_session'] = {
            'session_id': 'exact-session', 'session_file': str(transcript),
            'machine': 'machine', 'worktree': str(self.fixture.workspace.path)}
        return transcript

    def server(self, *, polling=False):
        server = LifecycleServer(self.controller, self.config, self.health_path)
        self.addCleanup(server.close)
        if polling:
            server.start()
        return server

    def wait_for(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail('Project title did not reconcile during service polling')

    def test_registered_legacy_session_title_updates_during_actual_polling(self):
        transcript = self.legacy_session()
        before = transcript.read_bytes()
        metadata = copy.deepcopy(self.authority.row['metadata'])
        terminals = copy.deepcopy(self.hosting.terminals)
        server = self.server(polling=True)
        self.wait_for(lambda: self.hosting.rows[0]['name'] == 'Original title')
        self.authority.row['title'] = 'Edited: title révisé'
        self.wait_for(lambda: self.hosting.rows[0]['name'] == 'Edited: title révisé')
        self.wait_for(lambda: server.health['projects'] and not server.health['projects'][0]['errors'])
        self.assertEqual(self.fixture.store.documents(), [])
        self.assertEqual(self.authority.row['metadata'], metadata)
        self.assertEqual(transcript.read_bytes(), before)
        self.assertEqual(self.hosting.terminals, terminals)
        self.assertEqual(self.hosting.rows[0]['id'], 'retained-space')
        self.assertEqual(self.hosting.rows[0]['tabs'][0]['id'], 'retained-tab')
        self.assertEqual(self.hosting.rows[0]['folder'], metadata['omp_session']['worktree'])
        self.assertEqual(self.hosting.rows[1]['name'], 'Remote title')
        self.assertEqual(self.hosting.rows[2]['name'], 'Other title')
        self.assertTrue(self.commands)
        self.assertTrue(all(argv[0] in ('/configured/hc', '/configured/herdr-app') for argv in self.commands))

    def test_released_controller_run_without_metadata_mirror_updates_during_polling(self):
        self.fixture.start()
        released = self.fixture.call('release', {'expected': self.fixture.condition(), 'reason': 'owner request'})
        self.assertEqual(released['outcome'], 'accepted', released)
        self.fixture.drain()
        run = self.fixture.status()['run']
        self.assertEqual(run['release']['state'], 'released')
        self.assertNotIn('omp_session', self.authority.row['metadata'])
        # No discoverable legacy terminal: only the retained durable run binds
        # this exact workspace, including its released terminal/session IDs.
        self.hosting.terminals.clear()
        identities = {key: copy.deepcopy(run[key]) for key in ('scope', 'fence', 'workspace', 'release')}
        execution = copy.deepcopy(run['execution'])
        head = git(self.fixture.workspace.path, 'rev-parse', 'HEAD')
        branch = git(self.fixture.workspace.path, 'symbolic-ref', 'HEAD')
        server = self.server(polling=True)
        self.wait_for(lambda: self.hosting.rows[0]['name'] == 'Original title')
        self.authority.row['title'] = 'After release'
        self.wait_for(lambda: self.hosting.rows[0]['name'] == 'After release')
        current = self.fixture.status()['run']
        self.assertEqual({key: current[key] for key in identities}, identities)
        self.assertEqual(current['execution']['session'], execution['session'])
        self.assertEqual(current['execution']['terminal'], execution['terminal'])
        self.assertIsNone(server.failure)
        self.assertNotIn('omp_session', self.authority.row['metadata'])
        self.assertEqual(git(self.fixture.workspace.path, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.fixture.workspace.path, 'symbolic-ref', 'HEAD'), branch)

    def test_superseded_attempt_space_reconciles_without_running_pending_attempt(self):
        self.fixture.start()
        self.fixture.call('release', {'expected': self.fixture.condition(), 'reason': 'new work'})
        self.fixture.drain()
        old = self.fixture.status()['run']
        plan = self.fixture.workspace.plan
        self.fixture.workspace.plan = lambda run: {
            **plan(run), 'path': str(self.fixture.home / 'next-workspace'),
            'branchRef': 'refs/heads/next-attempt'}
        request = self.fixture.start_request()
        request['params'].update(newAttempt=True, expected=self.fixture.condition())
        accepted = self.controller.handle(request)
        self.assertEqual(accepted['outcome'], 'accepted', accepted)
        self.assertIsNone(accepted['run']['workspace'])
        records = {path: path.read_bytes() for path in self.fixture.store.root.glob('*.json')}
        self.authority.row['title'] = 'New title for retained history'
        result = self.server()._projects()
        self.assertEqual(result[0]['renamedSpaces'], ['retained-space'])
        self.assertEqual(result[0]['errors'], [])
        self.assertEqual(self.hosting.rows[0]['name'], 'New title for retained history')
        self.assertEqual(self.hosting.rows[0]['folder'], old['workspace']['path'])
        self.assertEqual({path: path.read_bytes() for path in records}, records)
        self.assertFalse((self.fixture.home / 'next-workspace').exists())

    def test_project_naming_does_not_write_controller_state(self):
        self.fixture.start()
        records = {path: path.read_bytes() for path in self.fixture.store.root.glob('*.json')}
        run = self.fixture.status()['run']
        self.authority.row['title'] = 'Presentation only'
        rows = self.server()._projects()
        self.assertEqual(rows[0]['renamedSpaces'], ['retained-space'])
        self.assertEqual(rows[0]['errors'], [])
        self.assertEqual({path: path.read_bytes() for path in records}, records)
        self.assertEqual(self.fixture.status()['run'], run)

    def test_epic_observation_failure_does_not_block_project_title_refresh(self):
        self.legacy_session()
        bd = self.authority.bd

        def epic_failure(scope, *args):
            if args[0] == 'list':
                raise beads_identity.Refusal('evidence_unavailable', 'Epic list unavailable')
            return bd(scope, *args)

        self.authority.bd = epic_failure
        server = self.server()
        server.reconcile_once()
        self.assertEqual(self.hosting.rows[0]['name'], 'Original title')
        self.assertIn('Epic list unavailable', server.health['projects'][0]['errors'][0]['error'])
        self.assertIsNone(server.failure)

    def test_rename_failure_is_project_health_and_next_poll_can_recover(self):
        self.legacy_session()
        self.hosting.rename_error = beads_identity.Refusal('evidence_unavailable', 'Herdr refused rename')
        server = self.server(polling=True)
        self.wait_for(lambda: server.health['projects'] and server.health['projects'][0]['errors'])
        self.assertIn('Herdr refused rename', server.health['projects'][0]['errors'][0]['error'])
        self.assertEqual(self.hosting.rows[0]['name'], 'collision-1')
        self.assertEqual(server.health['state'], 'running')
        self.hosting.rename_error = None
        self.wait_for(lambda: self.hosting.rows[0]['name'] == 'Original title')
        self.wait_for(lambda: not server.health['projects'][0]['errors'])
        self.assertEqual(self.fixture.store.documents(), [])



if __name__ == '__main__':
    unittest.main()

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


if __name__ == '__main__':
    unittest.main()

"""Consumer-visible lifecycle regressions with real disposable Git resources.

The deterministic execution boundary writes durable receipts rather than starting
omp. Real omp launch/resume acceptance belongs to the concrete adapter task.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import shlex
import sys
import tempfile
import threading
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).parent / 'bin'))
from beads_contract import Refusal, now, validate
from beads_controller import Controller, CAPABILITIES
from beads_identity import repository_identity
from beads_store import Store


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.PIPE, text=True).strip()


class Authority:
    def __init__(self, scope):
        self.scope = scope
        self.row = {'id': scope['issueId'], 'status': 'open', 'assignee': None, 'issue_type': 'task', 'metadata': {'unrelated': {'keep': True}}}

    def validate_scope(self, scope, machine):
        if scope['machineId'] != machine:
            raise Refusal('remote_machine', 'foreign machine')
        if scope != self.scope:
            raise Refusal('database_mismatch', 'foreign scope')

    def owner(self, scope):
        return 'owner'

    def issue(self, scope):
        return copy.deepcopy(self.row)

    def eligible(self, scope, owner):
        if self.row['status'] not in ('open', 'in_progress') or self.row['assignee'] not in (None, owner):
            raise Refusal('issue_not_ready', 'not eligible')
        return self.issue(scope)

    def perform(self, step, run, intent, private):
        self.row.update(status='in_progress', assignee='owner')
        return self.recover(step, run, intent, private)

    def recover(self, step, run, intent, private):
        return {'status': self.row['status'], 'assignee': self.row['assignee'], 'observedAt': now()}


class Workspace:
    def __init__(self, root, path):
        self.root, self.path = root, path

    def plan(self, run):
        return {'provider': 'omp-worktree', 'backing': 'linked', 'path': str(self.path),
                'branchRef': 'refs/heads/task', 'sourceRepository': run['scope']['repository'],
                'commonDir': run['scope']['repository']['commonDir']}

    def perform(self, step, run, intent, private):
        git(self.root, 'worktree', 'add', '-b', 'task', str(self.path))
        return self.recover(step, run, intent, private)

    def recover(self, step, run, intent, private):
        if not self.path.exists():
            return None
        return {'workspace': self.plan(run)}

    def validate(self, run):
        rows = git(self.root, 'worktree', 'list', '--porcelain')
        if 'worktree ' + run['workspace']['path'] not in rows:
            raise Refusal('workspace_mismatch', 'registry changed')


class Execution:
    capabilities = CAPABILITIES

    def __init__(self, receipt_directory):
        self.directory = receipt_directory
        self.launches = 0
        self.crash = False
        self.uncertain = False

    def perform(self, step, run, intent, private):
        if step in ('launch', 'resume'):
            self.launches += 1
            event = {'kind': 'session_bound', 'session': {'harness': 'omp', 'sessionId': 'actual-session', 'bindingId': private['bindingId']},
                     'terminal': {'machineId': run['scope']['machineId'], 'address': 'term-1', 'spaceId': None, 'tabId': None}, 'activity': 'active'}
        elif step == 'release':
            event = {'kind': 'release_acknowledged', 'receipt': {'receiptId': str(uuid.uuid4()), 'fence': run['fence'],
                     'session': run['execution']['session'], 'releasedAt': now(), 'acknowledgedBy': run['owner']}}
        elif step == 'read_only':
            event = {'kind': 'read_only_acknowledged', 'session': run['execution']['session']}
        else:
            return {'focused': True}
        receipt = {'observation': {'fence': run['fence'], 'bindingId': private['bindingId'], 'eventId': str(uuid.uuid4()),
                                  'sequence': private['sequence'] + 1, 'observedAt': now(), 'event': event}}
        (self.directory / intent['operationId']).write_text(json.dumps(receipt))
        if self.crash:
            raise SystemExit('interruption after external effect, before controller receipt')
        return receipt

    def recover(self, step, run, intent, private):
        if self.uncertain:
            return None
        path = self.directory / intent['operationId']
        return json.loads(path.read_text()) if path.exists() else None

    def validate_observation(self, run, observation, private):
        # Deterministic adapter proof: the allocated terminal/session must match.
        event = observation['event']
        if event['kind'] == 'session_bound' and event['session']['sessionId'] != 'actual-session':
            raise Refusal('session_mismatch', 'not actual durable session')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-b', 'master')
        git(self.repo, 'config', 'user.name', 'test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'source').write_text('initial\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'initial')
        self.scope = {'repository': repository_identity(str(self.repo)), 'beadsDir': str(self.repo / '.beads'),
                      'issueId': 'collision-1', 'machineId': 'machine'}
        self.target = {'mode': 'local-target', 'repository': self.scope['repository'], 'ref': 'refs/heads/master'}
        self.store = Store(self.home / 'state')
        self.authority = Authority(self.scope)
        self.workspace = Workspace(self.repo, self.home / 'workspace')
        self.execution = Execution(self.home)
        self.controller = self.restart()

    def restart(self):
        return Controller('machine', Store(self.home / 'state'), self.authority, self.execution, self.workspace)

    def request(self, method, params=None, request_id=None):
        return {'apiVersion': 1, 'requestId': request_id or str(uuid.uuid4()), 'scope': copy.deepcopy(self.scope),
                'method': method, 'params': params if params is not None else {}}

    def call(self, method, params=None, request_id=None):
        response = self.controller.handle(self.request(method, params, request_id))
        validate('controller', response, 'Response')
        return response

    def status(self):
        return self.call('status', {'runId': None})

    def condition(self):
        run = self.status()['run']
        return {'fence': run['fence'], 'revision': run['revision']}

    def start_request(self):
        return self.request('start_or_focus', {'expected': None, 'harness': 'omp', 'target': copy.deepcopy(self.target),
                'newAttempt': False, 'policy': {'closeAfterIntegration': False, 'cleanupAfterRelease': False}})

    def start(self):
        result = self.controller.handle(self.start_request())
        self.assertEqual(result['outcome'], 'accepted', result)
        self.drain()
        self.assertEqual(self.status()['run']['execution']['state'], 'idle')
        return result

    def drain(self):
        for _ in range(10):
            if not self.controller.work_once():
                return
        self.fail('worker did not quiesce')

    def handoff(self, target=None, revision=1, submission_id='source'):
        target = target or self.target
        path = self.workspace.path
        (path / 'source').write_text('submitted\n')
        git(path, 'add', '.')
        git(path, 'commit', '--allow-empty', '-m', 'submitted')
        head = git(path, 'rev-parse', 'HEAD')
        identity = {'submissionId': submission_id, 'submissionRevision': revision, 'head': head,
                    'target': target, 'pullRequest': None if target['mode'] != 'github-pr' else {'number': 1, 'url': 'https://github.com/owner/repo/pull/1'}}
        result = self.call('handoff', {'expected': self.condition(), 'previousCoverageRevision': self.status()['run']['coverage']['revision'],
              'target': target, 'workspaceHead': head, 'requiredSubmissions': [identity], 'summary': 'ready', 'nextSteps': [], 'risks': []})
        self.assertEqual(result['outcome'], 'ok', result)
        return identity

    def reconcile(self):
        result = self.call('reconcile', {'expected': self.condition()})
        self.assertEqual(result['outcome'], 'accepted', result)
        self.drain()
        return self.status()['run']

    def test_concurrent_starts_reserve_exactly_one_active_attempt(self):
        barrier = threading.Barrier(3)
        results = []
        def start():
            barrier.wait()
            results.append(self.restart().handle(self.start_request()))
        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(r['outcome'] == 'accepted' for r in results), 1, results)
        self.drain()
        self.assertEqual(self.execution.launches, 1)
        self.assertEqual(len(self.store.read(self.scope)['runs']), 1)

    def test_retry_same_request_resolves_original_operation_after_restart(self):
        request = self.start_request()
        first = self.controller.handle(request)
        self.controller = self.restart()
        self.assertEqual(self.controller.handle(request)['operation']['operationId'], first['operation']['operationId'])
        changed = copy.deepcopy(request)
        changed['params']['target']['ref'] = 'refs/heads/other'
        self.assertEqual(self.controller.handle(changed)['error']['code'], 'request_id_conflict')
        self.drain()
        completed = self.controller.handle(request)
        self.assertEqual(completed['operation']['state'], 'succeeded')
        self.assertEqual(self.execution.launches, 1)

    def test_restart_recovers_observed_launch_without_second_external_launch(self):
        self.controller.handle(self.start_request())
        self.controller.work_once()  # claim
        self.controller.work_once()  # workspace
        self.execution.crash = True
        with self.assertRaises(SystemExit):
            self.controller.work_once()
        self.controller = self.restart()
        self.execution.crash = False
        self.drain()
        self.assertEqual(self.execution.launches, 1)
        self.assertEqual(self.status()['run']['execution']['session']['sessionId'], 'actual-session')

    def test_uncertain_launch_stays_pending_and_never_retries_launch(self):
        self.controller.handle(self.start_request())
        self.controller.work_once()
        self.controller.work_once()
        self.execution.crash = True
        with self.assertRaises(SystemExit):
            self.controller.work_once()
        self.execution.uncertain = True
        self.controller = self.restart()
        for _ in range(3):
            self.controller.work_once()
        status = self.status()
        self.assertEqual(status['operation']['state'], 'running')
        self.assertEqual(status['operation']['error']['code'], 'pending_operation')
        self.assertEqual(self.execution.launches, 1)

    def test_stale_revision_and_foreign_machine_cannot_mutate(self):
        self.start()
        condition = self.condition()
        condition['revision'] -= 1
        result = self.call('release', {'expected': condition, 'reason': 'done'})
        self.assertEqual(result['error']['code'], 'stale_revision')
        request = self.request('status', {'runId': None})
        request['scope']['machineId'] = 'foreign'
        self.assertEqual(self.controller.handle(request)['error']['code'], 'remote_machine')
        self.assertEqual(self.status()['run']['release']['state'], 'held')

    def test_no_remote_local_merge_produces_display_only_manual_command(self):
        self.start()
        identity = self.handoff()
        self.assertEqual(git(self.repo, 'remote'), '')
        git(self.repo, 'merge', '--ff-only', identity['head'])
        run = self.reconcile()
        self.assertEqual(run['coverage']['state'], 'integrated')
        self.assertEqual(run['closure']['reason'], 'Merged — awaiting manual closure')
        self.assertEqual(run['closure']['state'], 'awaiting_manual')
        self.assertEqual(self.authority.row['status'], 'in_progress')
        self.assertEqual(self.authority.row['metadata'], {'unrelated': {'keep': True}})
        self.controller = self.restart()
        self.assertEqual(self.status()['run']['submissions'][0]['identity']['head'], identity['head'])
        self.assertEqual(self.status()['run']['cleanup']['blockers'], ['manual_cleanup_required'])
        self.assertTrue(self.workspace.path.exists())

    def test_external_close_reopen_does_not_reclose_or_delete(self):
        self.start()
        identity = self.handoff()
        git(self.repo, 'merge', '--ff-only', identity['head'])
        self.reconcile()
        self.authority.row['status'] = 'closed'
        closed = self.reconcile()
        self.assertEqual(closed['closure']['state'], 'observed_closed')
        self.assertIsNone(closed['closure']['manualCommand'])
        self.authority.row['status'] = 'open'
        reopened = self.reconcile()
        self.assertEqual(reopened['disposition'], 'reopened')
        self.assertNotEqual(reopened['coverage']['state'], 'integrated')
        self.assertIsNone(reopened['closure']['manualCommand'])
        self.assertEqual(self.authority.row['status'], 'open')
        self.assertTrue(self.workspace.path.exists())

    def test_target_reset_invalidates_proof_but_preserves_audit(self):
        self.start()
        old_target = git(self.repo, 'rev-parse', 'master')
        identity = self.handoff()
        git(self.repo, 'merge', '--ff-only', identity['head'])
        integrated = self.reconcile()
        evidence = integrated['submissions'][0]['evidence']
        git(self.repo, 'reset', '--hard', old_target)
        held = self.reconcile()
        self.assertNotEqual(held['coverage']['state'], 'integrated')
        self.assertIsNone(held['closure']['manualCommand'])
        self.assertTrue(any(entry.get('submissions', [{}])[0].get('evidence') == evidence for entry in self.store.read(self.scope)['audit'] if entry.get('submissions')))

    def test_pr_record_survives_restart_without_local_provider_requirements(self):
        self.start()
        target = {'mode': 'github-pr', 'repository': {'host': 'github.com', 'repositoryId': '123', 'owner': 'owner', 'name': 'repo'}, 'ref': 'refs/heads/master'}
        identity = self.handoff(target)
        self.controller = self.restart()
        run = self.status()['run']
        self.assertEqual(run['target'], target)
        self.assertEqual(run['submissions'][0]['identity'], identity)
        self.assertNotIn('pullRequest', run['target'])

    def test_release_retains_resources_and_rejects_resume(self):
        self.start()
        result = self.call('release', {'expected': self.condition(), 'reason': 'explicit owner request'})
        self.assertEqual(result['outcome'], 'accepted')
        self.drain()
        run = self.status()['run']
        self.assertEqual(run['release']['state'], 'released')
        self.assertEqual(run['cleanup']['state'], 'held')
        self.assertTrue(self.workspace.path.exists())
        result = self.call('resume', {'expected': self.condition(), 'access': 'writable'})
        self.assertEqual(result['error']['code'], 'run_released')

    def test_unsupported_adapter_refuses_before_reservation(self):
        controller = Controller('machine', self.store, self.authority)
        result = controller.handle(self.start_request())
        self.assertEqual(result['error']['code'], 'unsupported_capability')
        self.assertIsNone(self.store.read(self.scope)['current'])

    def test_policy_cannot_enable_close_or_delete(self):
        for name in ('closeAfterIntegration', 'cleanupAfterRelease'):
            request = self.start_request()
            request['params']['policy'][name] = True
            self.assertEqual(self.controller.handle(request)['error']['code'], 'invalid_request')
        self.assertIsNone(self.store.read(self.scope)['current'])

    def test_receipt_from_socket_or_other_submission_is_not_trusted(self):
        self.start()
        identity = self.handoff()
        run = self.status()['run']
        receipt = {'receiptId': 'receipt', 'fence': run['fence'], 'submission': {'submissionId': 'source', 'submissionRevision': 1},
                   'head': identity['head'], 'target': self.target, 'resultCommit': identity['head'], 'attestedBy': 'owner', 'attestedAt': now()}
        request = self.request('confirm_integration', {'expected': self.condition(), 'receipt': receipt})
        self.assertEqual(self.controller.handle(request)['error']['code'], 'untrusted_receipt')
        receipt['head'] = 'a' * 40
        self.assertEqual(self.controller.handle(self.request('confirm_integration', {'expected': self.condition(), 'receipt': receipt}), trusted_human=True)['error']['code'], 'submission_changed')

    def test_invalid_fresh_evidence_revokes_previously_displayed_manual_handoff(self):
        self.start()
        identity = self.handoff()
        git(self.repo, 'merge', '--ff-only', identity['head'])
        integrated = self.reconcile()
        invalid = copy.deepcopy(integrated['submissions'][0])
        invalid['evidence']['head'] = 'a' * 40
        class CorruptObserver:
            def observe(self, run, receipts):
                return [invalid]
        self.controller.integration = CorruptObserver()
        result = self.reconcile()
        self.assertNotEqual(result['coverage']['state'], 'integrated')
        self.assertIsNone(result['closure']['manualCommand'])
        self.assertEqual(self.status()['operation']['error']['code'], 'evidence_invalid')
        self.assertEqual(self.authority.row['status'], 'in_progress')

    def test_callback_racing_worker_receipt_keeps_private_adapter_association(self):
        original = self.execution.perform
        def perform(step, run, intent, private):
            receipt = original(step, run, intent, private)
            if step == 'launch':
                observed = self.controller.handle(self.request('observe', {
                    'credential': private['credential'], 'observation': receipt['observation']}))
                self.assertEqual(observed['outcome'], 'ok', observed)
                receipt['private'] = {'durableAssociation': 'actual-session-file'}
            return receipt
        self.execution.perform = perform
        self.start()
        run_id = self.status()['run']['fence']['runId']
        self.controller = self.restart()
        self.assertEqual(self.store.read(self.scope)['private'][run_id]['adapter']['durableAssociation'], 'actual-session-file')
        self.assertEqual(self.execution.launches, 1)

    def test_prelaunch_failure_can_be_explicitly_released_without_workspace(self):
        def fail(step, run, intent, private):
            raise Refusal('workspace_missing', 'allocation failed before acquiring resources')
        self.workspace.perform = fail
        self.controller.handle(self.start_request())
        self.drain()
        self.assertIsNone(self.status()['run']['workspace'])
        result = self.call('release', {'expected': self.condition(), 'reason': 'reviewed failed reservation'})
        self.assertEqual(result['outcome'], 'accepted', result)
        self.drain()
        self.assertEqual(self.status()['run']['release']['state'], 'released')
        self.assertEqual(self.execution.launches, 0)

    def test_reassignment_enforces_read_only_and_original_owner_can_release(self):
        self.start()
        old = self.status()['run']
        self.authority.row['assignee'] = 'new-owner'
        revoked = self.reconcile()
        self.assertEqual(revoked['disposition'], 'reassigned')
        self.assertGreater(revoked['fence']['generation'], old['fence']['generation'])
        self.assertTrue(revoked['execution']['readOnlyAcknowledged'])
        release = self.call('release', {'expected': self.condition(), 'reason': 'original owner relinquishes'})
        self.assertEqual(release['outcome'], 'accepted', release)
        self.drain()
        self.assertEqual(self.status()['run']['release']['state'], 'released')
        self.assertEqual(self.authority.row['assignee'], 'new-owner')
        self.assertTrue(self.workspace.path.exists())

    def test_new_attempt_isolated_and_old_callback_cannot_mutate_it(self):
        self.start()
        old = self.status()['run']
        private = copy.deepcopy(self.store.read(self.scope)['private'][old['fence']['runId']])
        self.call('release', {'expected': self.condition(), 'reason': 'new work'})
        self.drain()
        old_workspace = self.workspace.path
        self.workspace = Workspace(self.repo, self.home / 'workspace-two')
        self.workspace.plan = lambda run: {
            'provider': 'omp-worktree', 'backing': 'linked', 'path': str(self.workspace.path),
            'branchRef': 'refs/heads/task-two', 'sourceRepository': run['scope']['repository'],
            'commonDir': run['scope']['repository']['commonDir']}
        def allocate(step, run, intent, private):
            git(self.repo, 'worktree', 'add', '-b', 'task-two', str(self.workspace.path))
            return {'workspace': self.workspace.plan(run)}
        self.workspace.perform = allocate
        self.controller.workspace = self.workspace
        request = self.start_request()
        request['params'].update(newAttempt=True, expected=self.condition())
        accepted = self.controller.handle(request)
        self.assertEqual(accepted['outcome'], 'accepted', accepted)
        self.drain()
        current = self.status()['run']
        self.assertEqual(current['fence']['attempt'], old['fence']['attempt'] + 1)
        self.assertEqual(current['submissions'], [])
        self.assertTrue(old_workspace.exists())
        observation = {'fence': old['fence'], 'bindingId': private['bindingId'], 'eventId': 'late',
            'sequence': private['sequence'] + 1, 'observedAt': now(),
            'event': {'kind': 'activity', 'activity': 'active', 'turn': 'working'}}
        late = self.call('observe', {'credential': private['credential'], 'observation': observation})
        self.assertEqual(late['error']['code'], 'stale_generation')
        self.assertEqual(self.status()['run']['fence'], current['fence'])

    def test_workspace_branch_mismatch_refuses_release_without_cleanup(self):
        self.start()
        git(self.workspace.path, 'checkout', '-b', 'different')
        result = self.call('release', {'expected': self.condition(), 'reason': 'done'})
        self.assertEqual(result['error']['code'], 'workspace_mismatch')
        self.assertEqual(self.status()['run']['release']['state'], 'held')
        self.assertTrue(self.workspace.path.exists())

    def test_scope_contention_defers_worker_instead_of_killing_service(self):
        self.controller.handle(self.start_request())
        locked, finish = threading.Event(), threading.Event()
        def hold():
            with self.store.transaction(self.scope):
                locked.set()
                finish.wait(3)
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(locked.wait(3))
            self.assertFalse(self.controller.work_once())
        finally:
            finish.set()
            thread.join()
        self.drain()
        self.assertEqual(self.execution.launches, 1)

    def test_start_resumes_exact_session_after_validated_exit_and_rejects_old_binding(self):
        self.start()
        run = self.status()['run']
        private = self.store.read(self.scope)['private'][run['fence']['runId']]
        observation = {'fence': run['fence'], 'bindingId': private['bindingId'], 'eventId': 'exit',
            'sequence': private['sequence'] + 1, 'observedAt': now(),
            'event': {'kind': 'exited', 'exitCode': 0, 'activity': 'inactive'}}
        self.assertEqual(self.call('observe', {'credential': private['credential'], 'observation': observation})['outcome'], 'ok')
        request = self.start_request()
        request['params']['expected'] = self.condition()
        accepted = self.controller.handle(request)
        self.assertEqual(accepted['outcome'], 'accepted', accepted)
        self.drain()
        resumed = self.status()
        self.assertEqual(resumed['operation']['state'], 'succeeded')
        self.assertEqual(resumed['run']['execution']['session']['sessionId'], run['execution']['session']['sessionId'])
        self.assertGreater(resumed['run']['fence']['generation'], run['fence']['generation'])
        stale = self.call('observe', {'credential': private['credential'], 'observation': observation})
        self.assertEqual(stale['error']['code'], 'stale_generation')

    def test_squash_requires_owner_receipt_and_independent_target_commit_proof(self):
        self.start()
        identity = self.handoff()
        git(self.repo, 'merge', '--squash', identity['head'])
        git(self.repo, 'commit', '-m', 'operator squash')
        incomplete = self.reconcile()
        self.assertEqual(incomplete['submissions'][0]['state'], 'needs_confirmation')
        self.assertIsNone(incomplete['closure']['manualCommand'])
        receipt = {'receiptId': 'reviewed-squash', 'fence': incomplete['fence'],
            'submission': {'submissionId': 'source', 'submissionRevision': 1}, 'head': identity['head'],
            'target': self.target, 'resultCommit': git(self.repo, 'rev-parse', 'HEAD'), 'attestedBy': 'owner', 'attestedAt': now()}
        result = self.controller.handle(self.request('confirm_integration', {'expected': self.condition(), 'receipt': receipt}), trusted_human=True)
        self.assertEqual(result['outcome'], 'ok', result)
        integrated = self.reconcile()
        self.assertEqual(integrated['coverage']['state'], 'integrated')
        self.assertEqual(integrated['submissions'][0]['evidence']['proof'], 'receipt')
        self.assertEqual(self.authority.row['status'], 'in_progress')


    def test_manual_command_quotes_issue_and_pins_canonical_database_and_checkout(self):
        self.scope['issueId'] = "full-id'; printf injected; #"
        self.authority.row['id'] = self.scope['issueId']
        self.start()
        identity = self.handoff()
        git(self.repo, 'merge', '--ff-only', identity['head'])
        run = self.reconcile()
        argv = shlex.split(run['closure']['manualCommand'])
        self.assertEqual(argv[0], 'BEADS_DIR=' + self.scope['beadsDir'])
        self.assertEqual(argv[1], 'bd')
        self.assertEqual(argv[argv.index('-C') + 1], self.scope['repository']['root'])
        self.assertEqual(argv[-2:], ['--', self.scope['issueId']])
        self.assertNotIn('--force', argv)
        self.assertIn(run['fence']['runId'], argv[argv.index('--reason') + 1])

    def test_remote_authority_is_not_downgraded_to_local_integration(self):
        self.start()
        remote = self.home / 'published.git'
        target = {'mode': 'remote-target', 'repository': self.scope['repository'],
                  'remoteUrl': str(remote), 'ref': 'refs/heads/master'}
        identity = self.handoff(target)
        git(self.home, 'clone', '--bare', str(self.repo), str(remote))
        git(self.repo, 'merge', '--ff-only', identity['head'])
        unpublished = self.reconcile()
        self.assertNotEqual(unpublished['coverage']['state'], 'integrated')
        self.assertIsNone(unpublished['closure']['manualCommand'])
        git(remote, 'update-ref', 'refs/heads/master', identity['head'])
        published = self.reconcile()
        evidence = published['submissions'][0]['evidence']
        self.assertEqual(published['coverage']['state'], 'integrated')
        self.assertEqual(evidence['kind'], 'remote-target')
        self.assertEqual(evidence['authorityUrl'], str(remote))

    def test_mode_change_drops_consumable_old_evidence_and_preserves_immutable_history(self):
        self.start()
        source = self.handoff()
        run = self.status()['run']
        base_head = git(self.repo, 'rev-parse', 'master')
        base = {'submissionId': 'already-integrated', 'submissionRevision': 1, 'head': base_head,
                'target': self.target, 'pullRequest': None}
        self.call('handoff', {'expected': self.condition(), 'previousCoverageRevision': run['coverage']['revision'],
            'target': self.target, 'workspaceHead': source['head'], 'requiredSubmissions': [base, source],
            'summary': 'complete set', 'nextSteps': [], 'risks': []})
        partial = self.reconcile()
        old = next(s for s in partial['submissions'] if s['identity']['submissionId'] == base['submissionId'])
        self.assertEqual(old['state'], 'integrated')
        self.assertNotEqual(partial['coverage']['state'], 'integrated')
        pr_target = {'mode': 'github-pr', 'repository': {'host': 'github.com', 'repositoryId': '123',
                    'owner': 'owner', 'name': 'repo'}, 'ref': 'refs/heads/master'}
        replacement = self.handoff(pr_target, revision=2)
        self.controller = self.restart()
        changed = self.status()['run']
        self.assertEqual(changed['coverage']['required'], [{'submissionId': replacement['submissionId'], 'submissionRevision': 2}])
        self.assertEqual(changed['coverage']['state'], 'pending')
        self.assertIsNone(changed['closure']['manualCommand'])
        self.assertEqual(next(s for s in changed['submissions'] if s['identity']['submissionId'] == base['submissionId'])['evidence'], old['evidence'])


    def test_service_reports_corrupt_record_identity_without_overwriting_it(self):
        from beads_transport import ControllerServer, TransportError
        with self.store.transaction(self.scope) as document:
            document['audit'].append({'seed': True})
        record = next(self.store.root.glob('*.json'))
        record.write_text('{corrupt')
        before = record.read_bytes()
        server = ControllerServer(self.controller, self.home / 'controller.sock')
        with self.assertRaises(TransportError) as raised:
            server.serve_forever()
        self.assertEqual(raised.exception.code, 'invalid_record')
        self.assertIn(str(record), str(raised.exception))
        self.assertEqual(record.read_bytes(), before)


class MachineIdentityTests(unittest.TestCase):
    def test_missing_or_malformed_machine_identity_is_never_replaced(self):
        from unittest.mock import patch
        from beads_identity import machine_id
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_STATE_HOME': directory}):
            path = Path(directory) / 'go-bd/machine-id'
            with self.assertRaises(Refusal) as missing:
                machine_id()
            self.assertEqual(missing.exception.code, 'unknown_machine')
            self.assertFalse(path.exists())
            path.parent.mkdir()
            path.write_text('corrupt-machine\\n')
            before = path.read_bytes()
            with self.assertRaises(Refusal) as malformed:
                machine_id()
            self.assertEqual(malformed.exception.code, 'unknown_machine')
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

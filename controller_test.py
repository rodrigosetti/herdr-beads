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


class LegacyExecution(Execution):
    def __init__(self, directory):
        super().__init__(directory)
        self.active = True
        self.focuses = 0
        self.interrupt_adoption = False
        self.adoption_error = None

    def adopt(self, run, intent, private):
        if self.interrupt_adoption:
            raise SystemExit('interrupted after durable adoption intent')
        if self.adoption_error:
            raise Refusal('session_mismatch', self.adoption_error)
        return {'workspacePath': str(self.directory / 'workspace'),
                'session': {'harness': 'omp', 'sessionId': 'actual-session', 'bindingId': private['bindingId']},
                'terminal': {'machineId': run['scope']['machineId'], 'address': 'legacy-terminal',
                             'spaceId': None, 'tabId': None} if self.active else None,
                'activity': 'active' if self.active else 'inactive',
                'private': {'preservedHistory': 'exact-adapter-identity'}}

    def inspect(self, run, private):
        return 'active' if self.active else 'inactive'

    def perform(self, step, run, intent, private):
        if step == 'focus':
            self.focuses += 1
        return super().perform(step, run, intent, private)


class LegacyWorkspace(Workspace):
    def adopt(self, run, path):
        if path != str(self.path):
            raise Refusal('workspace_mismatch', 'different retained workspace')
        return self.plan(run)


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

    def retained_start_request(self):
        request = self.start_request()
        request['params']['expected'] = self.condition()
        return request

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

    def legacy(self, active=True):
        git(self.repo, 'worktree', 'add', '-b', 'task', str(self.workspace.path))
        self.workspace = LegacyWorkspace(self.repo, self.workspace.path)
        self.execution = LegacyExecution(self.home)
        self.execution.active = active
        self.authority.row.update(status='in_progress', assignee='owner')
        self.controller = self.restart()
        return self.request('adopt_legacy', {'expected': None})

    def test_live_adoption_restart_and_repeated_focus_preserve_one_unbound_run(self):
        request = self.legacy()
        before = copy.deepcopy(self.authority.row)
        adoption = next(action for action in self.status()['actions'] if action['method'] == 'adopt_legacy')
        self.assertTrue(adoption['enabled'])
        self.assertEqual(self.controller.handle(request)['outcome'], 'accepted')
        self.execution.interrupt_adoption = True
        with self.assertRaises(SystemExit):
            self.controller.work_once()
        self.assertEqual(self.status()['operation']['state'], 'running')
        self.execution.interrupt_adoption = False
        self.controller = self.restart()
        self.drain()
        run = self.status()['run']
        self.assertIsNone(run['target'])
        self.assertEqual(run['execution']['session']['sessionId'], 'actual-session')
        first_id = run['fence']['runId']
        repeated = self.call('adopt_legacy', {'expected': self.condition()})
        self.assertEqual(repeated['outcome'], 'accepted', repeated)
        self.drain()
        self.assertEqual(self.status()['run']['fence']['runId'], first_id)
        self.assertEqual(self.execution.focuses, 2)
        self.assertEqual(self.execution.launches, 0)
        self.assertEqual(self.authority.row, before)
        doc = self.store.read(self.scope)
        self.assertEqual(len(doc['runs']), 1)
        self.assertEqual([e['kind'] for e in doc['audit']], ['adoption_intent', 'adoption_result'])
        self.assertEqual(run['coverage']['state'], 'unsubmitted')
        self.assertIsNone(self.reconcile()['closure']['manualCommand'])

    def test_inactive_adoption_resumes_same_session_without_binding_destination(self):
        request = self.legacy(active=False)
        git(self.repo, 'remote', 'add', 'origin', 'https://github.com/example/not-integration-evidence.git')
        self.assertEqual(self.controller.handle(request)['outcome'], 'accepted')
        self.drain()
        run = self.status()['run']
        self.assertEqual(run['execution']['state'], 'stopped')
        self.assertEqual(self.execution.launches, 0)
        response = self.call('start_or_focus', {'expected': self.condition(), 'harness': 'omp',
            'target': None, 'policy': run['policy'], 'newAttempt': False})
        self.assertEqual(response['outcome'], 'accepted', response)
        self.drain()
        resumed = self.status()['run']
        self.assertEqual(resumed['execution']['session']['sessionId'], run['execution']['session']['sessionId'])
        self.assertEqual(resumed['fence']['runId'], run['fence']['runId'])
        self.assertEqual(resumed['fence']['generation'], 2)
        self.assertIsNone(resumed['target'])
        self.assertEqual(self.execution.launches, 1)
        self.assertEqual(resumed['cleanup']['state'], 'held')

    def test_conflicting_adoption_remains_visible_and_retry_retains_attempt(self):
        request = self.legacy()
        self.execution.adoption_error = 'conflicting exact receipts'
        self.controller.handle(request)
        self.drain()
        failed = self.status()
        self.assertEqual(failed['operation']['error']['code'], 'session_mismatch')
        self.assertIsNone(failed['run']['execution']['session'])
        self.assertEqual(self.execution.launches, 0)
        self.assertTrue(next(action for action in failed['actions'] if action['method'] == 'adopt_legacy')['enabled'])
        self.execution.adoption_error = None
        result = self.call('adopt_legacy', {'expected': self.condition()})
        self.assertEqual(result['outcome'], 'accepted', result)
        self.drain()
        self.assertEqual(self.status()['run']['fence']['runId'], failed['run']['fence']['runId'])
        self.assertEqual(len(self.store.read(self.scope)['operations']), 2)

    def test_adoption_without_destination_requires_explicit_handoff_for_evidence(self):
        request = self.legacy()
        self.assertEqual(self.controller.handle(request)['outcome'], 'accepted')
        self.drain()
        run = self.reconcile()
        self.assertIsNone(run['target'])
        self.assertEqual(run['coverage']['required'], [])
        self.assertEqual(run['release']['state'], 'held')
        self.assertEqual(run['cleanup']['blockers'], ['manual_cleanup_required'])
        self.assertIsNone(run['closure']['manualCommand'])
        identity = self.handoff()
        self.assertEqual(self.status()['run']['target'], identity['target'])
        self.assertEqual(self.status()['run']['coverage']['state'], 'pending')
    def assert_manual_handoff(self, run):
        closure = run['closure']
        self.assertEqual(closure['state'], 'awaiting_manual')
        self.assertEqual(closure['reason'], 'Merged — awaiting manual closure')
        self.assertIsInstance(closure['manualCommand'], str)
        handoff = closure['handoff']
        self.assertEqual(set(handoff), {'handoffId', 'createdAt', 'scope', 'fence', 'owner',
            'coverageRevision', 'workspaceHead', 'target', 'submissions', 'evidenceReason', 'warning', 'review'})
        self.assertRegex(handoff['handoffId'], r'^[0-9a-f]{64}$')
        for name in ('scope', 'fence', 'owner', 'target'):
            self.assertEqual(handoff[name], run[name])
        self.assertEqual(handoff['coverageRevision'], run['coverage']['revision'])
        self.assertEqual(handoff['workspaceHead'], run['coverage']['workspaceHead'])
        required = run['coverage']['required']
        submissions = [s for s in run['submissions'] if {
            name: s['identity'][name] for name in ('submissionId', 'submissionRevision')} in required]
        self.assertCountEqual(handoff['submissions'], submissions)
        self.assertTrue(all(s['state'] == 'integrated' and s['evidence'] for s in handoff['submissions']))
        return copy.deepcopy(handoff)

    def assert_handoff_revoked(self, run, historical=None):
        self.assertIsNone(run['closure']['manualCommand'])
        self.assertIsNone(run['closure']['handoff'])
        if historical is not None:
            self.assertTrue(any(entry.get('closure', {}).get('handoff') == historical
                for entry in self.store.read(self.scope)['audit']))
        self.assertEqual(self.authority.row['metadata'], {'unrelated': {'keep': True}})
        self.assertTrue(self.workspace.path.exists())
        self.assertIn('worktree ' + str(self.workspace.path), git(self.repo, 'worktree', 'list', '--porcelain'))
        self.assertEqual(git(self.workspace.path, 'symbolic-ref', 'HEAD'), run['workspace']['branchRef'])

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
        snapshot = self.assert_manual_handoff(run)
        command = run['closure']['manualCommand']
        self.assertEqual(self.authority.row['status'], 'in_progress')
        self.assertEqual(self.authority.row['metadata'], {'unrelated': {'keep': True}})
        self.controller = self.restart()
        self.assertEqual(self.status()['run']['closure']['handoff'], snapshot)
        repeated = self.reconcile()
        self.assertEqual(repeated['closure']['handoff'], snapshot)
        self.assertEqual(repeated['closure']['manualCommand'], command)
        self.assertEqual(self.status()['run']['submissions'][0]['identity']['head'], identity['head'])
        self.assertEqual(self.status()['run']['cleanup']['blockers'], ['manual_cleanup_required'])
        self.assertTrue(self.workspace.path.exists())

    def test_external_close_reopen_does_not_reclose_or_delete(self):
        self.start()
        identity = self.handoff()
        git(self.repo, 'merge', '--ff-only', identity['head'])
        snapshot = self.assert_manual_handoff(self.reconcile())
        self.authority.row['status'] = 'closed'
        closed = self.reconcile()
        self.assertEqual(closed['closure']['state'], 'observed_closed')
        self.assertIsNone(closed['closure']['manualCommand'])
        self.assert_handoff_revoked(closed, snapshot)
        self.authority.row['status'] = 'open'
        reopened = self.reconcile()
        self.assertEqual(reopened['disposition'], 'reopened')
        self.assertNotEqual(reopened['coverage']['state'], 'integrated')
        self.assertIsNone(reopened['closure']['manualCommand'])
        self.assert_handoff_revoked(reopened, snapshot)
        self.assertEqual(self.authority.row['status'], 'open')
        self.assertTrue(self.workspace.path.exists())

    def test_target_reset_invalidates_proof_but_preserves_audit(self):
        self.start()
        old_target = git(self.repo, 'rev-parse', 'master')
        identity = self.handoff()
        git(self.repo, 'merge', '--ff-only', identity['head'])
        integrated = self.reconcile()
        evidence = integrated['submissions'][0]['evidence']
        snapshot = self.assert_manual_handoff(integrated)
        git(self.repo, 'reset', '--hard', old_target)
        held = self.reconcile()
        self.assertNotEqual(held['coverage']['state'], 'integrated')
        self.assertIsNone(held['closure']['manualCommand'])
        self.assert_handoff_revoked(held, snapshot)
        self.assertTrue(any(entry.get('submissions', [{}])[0].get('evidence') == evidence for entry in self.store.read(self.scope)['audit'] if entry.get('submissions')))

    def test_workspace_commit_during_provider_observation_cannot_complete_old_submission(self):
        self.start()
        identity = self.handoff()
        git(self.repo, 'merge', '--ff-only', identity['head'])
        original = self.controller.integration.observe
        def observe(run, receipts):
            result = original(run, receipts)
            (self.workspace.path / 'new-work').write_text('not submitted')
            git(self.workspace.path, 'add', '.')
            git(self.workspace.path, 'commit', '-m', 'unsubmitted during observation')
            return result
        self.controller.integration.observe = observe
        run = self.reconcile()
        self.assertNotEqual(run['coverage']['state'], 'integrated')
        self.assertIsNone(run['closure']['manualCommand'])
        self.assert_handoff_revoked(run)

    def test_issue_reassigned_during_observation_revokes_manual_handoff(self):
        self.start()
        identity = self.handoff()
        git(self.repo, 'merge', '--ff-only', identity['head'])
        snapshot = self.assert_manual_handoff(self.reconcile())
        original = self.controller.integration.observe
        def observe(run, receipts):
            result = original(run, receipts)
            self.authority.row['assignee'] = 'new-owner'
            return result
        self.controller.integration.observe = observe
        run = self.reconcile()
        self.assertEqual(run['disposition'], 'reassigned')
        self.assertIsNone(run['closure']['manualCommand'])
        self.assert_handoff_revoked(run, snapshot)

    def test_pr_record_survives_restart_without_local_provider_requirements(self):
        self.start()
        target = {'mode': 'github-pr', 'repository': {'host': 'github.com', 'repositoryId': '123', 'owner': 'owner', 'name': 'repo'}, 'ref': 'refs/heads/master'}
        identity = self.handoff(target)
        self.controller = self.restart()
        run = self.status()['run']
        self.assertEqual(run['target'], target)
        self.assertEqual(run['submissions'][0]['identity'], identity)
        self.assertNotIn('pullRequest', run['target'])

    def test_provider_nonmerge_stale_head_and_unavailable_proof_revoke_handoff(self):
        self.start()
        target = {'mode': 'github-pr', 'repository': {'host': 'github.com', 'repositoryId': '123',
                  'owner': 'owner', 'name': 'repo'}, 'ref': 'refs/heads/master'}
        identity = self.handoff(target)
        observation = {'state': 'integrated'}
        def provider(target, submitted, cwd):
            state = observation['state']
            if state == 'unavailable':
                raise Refusal('evidence_unavailable', 'Provider proof is unavailable.', True)
            if state != 'integrated':
                return state, None
            return state, {'targetCommit': identity['head'], 'pullRequest': identity['pullRequest'],
                'mergedAt': '2026-01-01T00:00:00Z', 'mergedHead': identity['head'],
                'mergeCommit': identity['head'], 'providerRecordId': 'PR_exact_record'}
        self.controller.integration.pull_request = provider
        for state in ('closed_unmerged', 'head_changed', 'unavailable'):
            with self.subTest(provider_state=state):
                observation['state'] = 'integrated'
                integrated = self.reconcile()
                snapshot = self.assert_manual_handoff(integrated)
                self.assertEqual(snapshot['submissions'][0]['evidence']['kind'], 'github-pr')
                observation['state'] = state
                invalidated = self.reconcile()
                self.assertNotEqual(invalidated['coverage']['state'], 'integrated')
                self.assert_handoff_revoked(invalidated, snapshot)
                if state == 'unavailable':
                    self.assertEqual(self.status()['operation']['error']['code'], 'evidence_unavailable')
                else:
                    self.assertEqual(invalidated['submissions'][0]['state'], state)
                self.assertEqual(self.authority.row['status'], 'in_progress')

    def test_handoff_snapshots_only_exact_required_revisions_and_rejects_late_submission(self):
        self.start()
        obsolete = self.handoff()
        current = self.handoff(revision=2)
        run = self.status()['run']
        base = {'submissionId': 'base', 'submissionRevision': 1,
                'head': git(self.repo, 'rev-parse', 'master'), 'target': self.target, 'pullRequest': None}
        result = self.call('handoff', {'expected': self.condition(),
            'previousCoverageRevision': run['coverage']['revision'], 'target': self.target,
            'workspaceHead': current['head'], 'requiredSubmissions': [base, current],
            'summary': 'exact complete set', 'nextSteps': [], 'risks': []})
        self.assertEqual(result['outcome'], 'ok', result)
        self.assert_handoff_revoked(self.status()['run'])
        git(self.repo, 'merge', '--ff-only', current['head'])
        integrated = self.reconcile()
        snapshot = self.assert_manual_handoff(integrated)
        self.assertCountEqual([s['identity'] for s in snapshot['submissions']], [current, base])
        self.assertNotIn(obsolete, [s['identity'] for s in snapshot['submissions']])
        command = integrated['closure']['manualCommand']
        replacement = copy.deepcopy(current)
        replacement['submissionRevision'] += 1
        refused = self.call('handoff', {'expected': self.condition(),
            'previousCoverageRevision': integrated['coverage']['revision'], 'target': self.target,
            'workspaceHead': current['head'], 'requiredSubmissions': [replacement],
            'summary': 'late replacement', 'nextSteps': [], 'risks': []})
        self.assertEqual(refused['error']['code'], 'run_read_only')
        self.assertEqual(self.status()['run']['closure']['handoff'], snapshot)
        self.assertEqual(self.status()['run']['closure']['manualCommand'], command)
        self.assertEqual(self.authority.row['status'], 'in_progress')
        self.assertEqual(self.authority.row['metadata'], {'unrelated': {'keep': True}})
        self.assertTrue(self.workspace.path.exists())

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

    def test_status_advertises_start_without_reserving_or_launching(self):
        response = self.status()
        self.assertIsNone(response['run'])
        self.assertIsNone(self.store.read(self.scope)['current'])
        self.assertEqual(self.execution.launches, 0)

    def test_status_start_availability_matches_each_required_capability(self):
        for missing in ('launch', 'exact_resume', 'durable_identity', 'owner_activity'):
            with self.subTest(capability=missing):
                self.execution.capabilities = [capability for capability in CAPABILITIES if capability != missing]
                action = self.status()['actions'][0]
                self.assertEqual(action, {'method': 'start_or_focus', 'enabled': False,
                                          'refusal': 'unsupported_capability'})
                refusal = self.controller.handle(self.start_request())
                self.assertEqual(refusal['error']['capability'], missing)
        self.assertIsNone(self.store.read(self.scope)['current'])
        self.assertEqual(self.execution.launches, 0)

    def test_retained_status_advertises_pending_and_capability_refusals(self):
        self.controller.handle(self.start_request())
        actions = self.status()['actions']
        self.assertTrue(all(not action['enabled'] and action['refusal'] == 'pending_operation'
                            for action in actions))
        self.drain()
        self.execution.capabilities = ['launch', 'exact_resume', 'durable_identity', 'owner_activity']
        actions = {action['method']: action for action in self.status()['actions']}
        self.assertTrue(actions['start_or_focus']['enabled'])
        self.assertEqual(actions['handoff']['refusal'], 'unsupported_capability')
        self.assertEqual(actions['release']['refusal'], 'unsupported_capability')
        self.assertEqual(actions['resume']['refusal'], 'session_owned')
        self.execution.capabilities = []
        actions = {action['method']: action for action in self.status()['actions']}
        self.assertEqual(actions['start_or_focus']['refusal'], 'unsupported_capability')
        self.assertTrue(actions['reconcile']['enabled'])


    def test_start_requires_only_concrete_execution_capabilities(self):
        self.execution.capabilities = ['launch', 'exact_resume', 'durable_identity', 'owner_activity']
        self.start()
        support = self.status()['support']
        self.assertEqual(support['harnesses'], ['omp'])
        self.assertEqual(support['execution']['unsupported'], ['structured_handoff', 'explicit_release', 'read_only'])
        focused = self.controller.handle(self.retained_start_request())
        self.assertEqual(focused['outcome'], 'accepted', focused)
        self.drain()
        self.assertEqual(self.status()['operation']['state'], 'succeeded')
        self.execution.inspect = lambda run, private: 'inactive'
        resumed = self.call('resume', {'expected': self.condition(), 'access': 'writable'})
        self.assertEqual(resumed['outcome'], 'accepted', resumed)
        self.drain()
        self.assertEqual(self.execution.launches, 2)
        read_only = self.call('resume', {'expected': self.condition(), 'access': 'read_only'})
        self.assertEqual(read_only['error']['code'], 'unsupported_capability')
        self.assertEqual(read_only['error']['capability'], 'read_only')
        released = self.call('release', {'expected': self.condition(), 'reason': 'unsupported'})
        self.assertEqual(released['error']['code'], 'unsupported_capability')
        self.assertEqual(released['error']['capability'], 'explicit_release')
        head = git(self.workspace.path, 'rev-parse', 'HEAD')
        handoff = self.call('handoff', {'expected': self.condition(), 'previousCoverageRevision': 0,
            'target': self.target, 'workspaceHead': head,
            'requiredSubmissions': [{'submissionId': 'source', 'submissionRevision': 1, 'head': head,
                                     'target': self.target, 'pullRequest': None}],
            'summary': 'unsupported', 'nextSteps': [], 'risks': []})
        self.assertEqual(handoff['error']['code'], 'unsupported_capability')
        self.assertEqual(handoff['error']['capability'], 'structured_handoff')

    def test_missing_required_start_capability_refuses_without_reservation(self):
        required = ['launch', 'exact_resume', 'durable_identity', 'owner_activity']
        for missing in required:
            with self.subTest(capability=missing):
                self.execution.capabilities = [capability for capability in required if capability != missing]
                result = self.controller.handle(self.start_request())
                self.assertEqual(result['error']['code'], 'unsupported_capability')
                self.assertEqual(result['error']['capability'], missing)
                self.assertIsNone(self.store.read(self.scope)['current'])
        self.assertEqual(self.execution.launches, 0)
        self.assertFalse(self.workspace.path.exists())

    def test_epic_guard_does_not_depend_on_custom_authority_eligibility(self):
        self.authority.row['issue_type'] = 'epic'
        self.authority.eligible = lambda scope, owner: self.authority.issue(scope)
        calls = []
        self.execution.preflight = lambda run: calls.append('preflight')
        self.workspace.plan = lambda run: calls.append('workspace')
        result = self.controller.handle(self.start_request())
        self.assertEqual(result['error']['code'], 'issue_not_ready')
        document = self.store.read(self.scope)
        self.assertIsNone(document['current'])
        self.assertEqual(document['runs'], {})
        self.assertEqual(document['operations'], {})
        self.assertEqual(calls, [])
        self.assertEqual(self.authority.row['status'], 'open')
        self.assertEqual(self.execution.launches, 0)
        self.assertFalse(self.workspace.path.exists())

    def test_preflight_refuses_before_new_run_claim_or_workspace_planning(self):
        calls = []
        def preflight(run):
            self.assertEqual(run['scope'], self.scope)
            self.assertEqual(run['owner'], 'owner')
            self.assertEqual(run['target'], self.target)
            self.assertIsNone(run['workspace'])
            calls.append('preflight')
            raise Refusal('session_owned', 'Fresh metadata already owns a session.')
        self.execution.preflight = preflight
        self.workspace.plan = lambda run: calls.append('workspace')
        result = self.controller.handle(self.start_request())
        self.assertEqual(result['error']['code'], 'session_owned')
        self.assertEqual(calls, ['preflight'])
        self.assertEqual(self.store.read(self.scope)['runs'], {})
        self.assertEqual(self.authority.row['status'], 'open')
        self.assertEqual(self.execution.launches, 0)

    def reject_worker_preflight(self):
        def preflight(run):
            raise Refusal('session_mismatch', 'Fresh metadata changed after reservation.')
        self.execution.preflight = preflight
        self.assertFalse(self.controller.work_once())
        self.assertEqual(self.status()['operation']['state'], 'failed')
        self.assertEqual(self.status()['operation']['error']['code'], 'session_mismatch')
        self.assertEqual(self.execution.launches, 0)

    def test_preflight_rechecks_metadata_before_claim(self):
        self.controller.handle(self.start_request())
        self.reject_worker_preflight()
        self.assertEqual(self.authority.row['status'], 'open')
        self.assertFalse(self.workspace.path.exists())

    def test_preflight_rechecks_metadata_before_workspace_provisioning(self):
        self.controller.handle(self.start_request())
        self.controller.work_once()
        self.reject_worker_preflight()
        self.assertEqual(self.authority.row['status'], 'in_progress')
        self.assertFalse(self.workspace.path.exists())

    def test_preflight_rechecks_metadata_before_launch(self):
        self.controller.handle(self.start_request())
        self.controller.work_once()
        self.controller.work_once()
        self.reject_worker_preflight()
        self.assertTrue(self.workspace.path.exists())

    def test_retained_start_and_resume_require_current_local_owner(self):
        self.start()
        run = self.status()['run']
        self.authority.owner = lambda scope: 'different-local-owner'
        inspections = []
        self.execution.inspect = lambda run, private: inspections.append(run) or 'inactive'
        requests = [self.retained_start_request(),
                    self.request('resume', {'expected': self.condition(), 'access': 'writable'})]
        for request in requests:
            with self.subTest(method=request['method']):
                result = self.controller.handle(request)
                self.assertEqual(result['error']['code'], 'owner_mismatch')
        self.assertEqual(inspections, [])
        self.assertEqual(self.status()['run']['fence'], run['fence'])
        self.assertEqual(self.execution.launches, 1)

    def test_inherited_blockers_prevent_focusing_active_writable_run(self):
        self.start()
        def blocked(scope, owner):
            raise Refusal('issue_blocked', 'Inherited parent blocker.')
        self.authority.eligible = blocked
        result = self.controller.handle(self.retained_start_request())
        self.assertEqual(result['error']['code'], 'issue_blocked')
        self.assertEqual(self.status()['operation']['state'], 'succeeded')
        self.assertEqual(self.execution.launches, 1)

    def test_preflight_checks_actual_run_before_resume_or_focus(self):
        self.start()
        run = self.status()['run']
        inspections = []
        checked = []
        def preflight(actual):
            self.assertEqual(actual, run)
            checked.append(actual['fence'])
            raise Refusal('session_mismatch', 'Exact metadata no longer matches.')
        self.execution.preflight = preflight
        self.execution.inspect = lambda run, private: inspections.append(run) or 'inactive'
        requests = [self.retained_start_request(),
                    self.request('resume', {'expected': self.condition(), 'access': 'writable'})]
        for request in requests:
            result = self.controller.handle(request)
            self.assertEqual(result['error']['code'], 'session_mismatch')
        self.assertEqual(len(checked), 2)
        self.assertEqual(inspections, [])
        self.assertEqual(self.status()['run']['fence'], run['fence'])

    def test_retained_start_refreshes_activity_before_exact_resume_and_binding(self):
        self.start()
        run = self.status()['run']
        private = self.store.read(self.scope)['private'][run['fence']['runId']]
        def inspect(actual, actual_private):
            self.assertEqual(actual['fence'], run['fence'])
            self.assertEqual(actual['execution']['session'], run['execution']['session'])
            self.assertEqual(actual_private, private)
            actual_private['credential'] = 'must-not-leak'
            return 'inactive'
        self.execution.inspect = inspect
        accepted = self.controller.handle(self.retained_start_request())
        self.assertEqual(accepted['outcome'], 'accepted', accepted)
        self.assertEqual(accepted['run']['fence']['generation'], run['fence']['generation'] + 1)
        self.drain()
        self.assertEqual(self.status()['run']['execution']['session']['sessionId'], 'actual-session')
        self.assertEqual(self.execution.launches, 2)

    def test_explicit_resume_refreshes_stale_active_activity(self):
        self.start()
        run = self.status()['run']
        inspected = []
        def inspect(actual, private):
            inspected.append(copy.deepcopy(actual))
            return 'inactive'
        self.execution.inspect = inspect
        accepted = self.call('resume', {'expected': self.condition(), 'access': 'writable'})
        self.assertEqual(accepted['outcome'], 'accepted', accepted)
        self.assertEqual(inspected[0]['fence'], run['fence'])
        self.assertEqual(accepted['run']['fence']['generation'], run['fence']['generation'] + 1)
        self.drain()
        self.assertEqual(self.execution.launches, 2)

    def test_refreshed_active_activity_focuses_instead_of_resuming_stale_inactive_run(self):
        self.start()
        with self.store.transaction(self.scope) as doc:
            doc['runs'][doc['current']]['execution']['activity'] = 'inactive'
        run = self.status()['run']
        self.execution.inspect = lambda actual, private: 'active'
        accepted = self.controller.handle(self.retained_start_request())
        self.assertEqual(accepted['outcome'], 'accepted', accepted)
        self.assertEqual(accepted['run']['fence'], run['fence'])
        self.assertEqual(accepted['run']['execution']['activity'], 'active')
        self.drain()
        self.assertEqual(self.execution.launches, 1)
        resumed = self.call('resume', {'expected': self.condition(), 'access': 'writable'})
        self.assertEqual(resumed['error']['code'], 'session_owned')

    def test_unknown_inspected_activity_refuses_start_and_resume(self):
        self.start()
        run = self.status()['run']
        self.execution.inspect = lambda actual, private: 'unknown'
        requests = [self.retained_start_request(),
                    self.request('resume', {'expected': self.condition(), 'access': 'writable'})]
        for request in requests:
            result = self.controller.handle(request)
            self.assertEqual(result['error']['code'], 'session_owned')
        self.assertEqual(self.status()['run']['fence'], run['fence'])
        self.assertEqual(self.execution.launches, 1)

    def test_inspection_io_failure_is_recoverable_refusal_without_resume(self):
        self.start()
        run = self.status()['run']
        def inspect(actual, private):
            raise OSError('terminal socket unavailable')
        self.execution.inspect = inspect
        result = self.controller.handle(self.retained_start_request())
        self.assertEqual(result['outcome'], 'refused')
        self.assertEqual(result['error']['code'], 'evidence_unavailable')
        self.assertTrue(result['error']['retryable'])
        self.assertEqual(self.status()['run']['fence'], run['fence'])
        self.assertEqual(self.execution.launches, 1)

    def test_preflight_io_failure_refuses_before_reservation(self):
        def preflight(run):
            raise OSError('metadata unavailable')
        self.execution.preflight = preflight
        result = self.controller.handle(self.start_request())
        self.assertEqual(result['outcome'], 'refused')
        self.assertEqual(result['error']['code'], 'evidence_unavailable')
        self.assertTrue(result['error']['retryable'])
        self.assertIsNone(self.store.read(self.scope)['current'])

    def test_adapter_io_failure_after_launch_remains_recoverable_without_reperform(self):
        original = self.execution.perform
        def perform(step, run, intent, private):
            receipt = original(step, run, intent, private)
            if step == 'launch':
                raise OSError('receipt transport interrupted after durable effect')
            return receipt
        self.execution.perform = perform
        self.controller.handle(self.start_request())
        self.controller.work_once()
        self.controller.work_once()
        self.assertFalse(self.controller.work_once())
        self.assertEqual(self.status()['operation']['state'], 'running')
        self.assertEqual(self.status()['operation']['error']['code'], 'evidence_unavailable')
        self.drain()
        self.assertEqual(self.status()['operation']['state'], 'succeeded')
        self.assertEqual(self.execution.launches, 1)

    def test_worker_io_failure_outside_adapter_remains_recoverable(self):
        self.start()
        run = self.status()['run']
        original = self.controller.reconcile
        def reconcile(actual, private):
            raise OSError('integration observation unavailable')
        self.controller.reconcile = reconcile
        accepted = self.call('reconcile', {'expected': self.condition()})
        self.assertEqual(accepted['outcome'], 'accepted', accepted)
        self.assertFalse(self.controller.work_once())
        status = self.status()
        self.assertEqual(status['operation']['state'], 'running')
        self.assertEqual(status['operation']['error']['code'], 'evidence_unavailable')
        operation = self.store.read(self.scope)['operations'][status['operation']['operationId']]
        self.assertTrue(operation['intent']['started'])
        self.assertEqual(status['run']['workspace'], run['workspace'])
        self.assertTrue(self.workspace.path.exists())
        self.controller.reconcile = original
        self.drain()
        self.assertEqual(self.status()['operation']['state'], 'succeeded')
        self.assertEqual(self.execution.launches, 1)

    def test_uncertain_launch_recovery_does_not_preflight_its_unreceipted_metadata_again(self):
        def preflight(run):
            if self.execution.launches:
                raise Refusal('session_owned', 'Metadata already contains launched session.')
        self.execution.preflight = preflight
        self.controller.handle(self.start_request())
        self.controller.work_once()
        self.controller.work_once()
        self.execution.crash = True
        with self.assertRaises(SystemExit):
            self.controller.work_once()
        self.execution.crash = False
        self.controller = self.restart()
        self.drain()
        self.assertEqual(self.status()['operation']['state'], 'succeeded')
        self.assertEqual(self.execution.launches, 1)

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
        snapshot = self.assert_manual_handoff(integrated)
        invalid = copy.deepcopy(integrated['submissions'][0])
        invalid['evidence']['head'] = 'a' * 40
        class CorruptObserver:
            def observe(self, run, receipts):
                return [invalid]
        self.controller.integration = CorruptObserver()
        result = self.reconcile()
        self.assertNotEqual(result['coverage']['state'], 'integrated')
        self.assertIsNone(result['closure']['manualCommand'])
        self.assert_handoff_revoked(result, snapshot)
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
                activity = copy.deepcopy(receipt['observation'])
                activity.update(eventId='early-turn', sequence=activity['sequence'] + 1)
                activity['event'] = {'kind': 'activity', 'activity': 'active', 'turn': 'working'}
                advanced = self.controller.handle(self.request('observe', {
                    'credential': private['credential'], 'observation': activity}))
                self.assertEqual(advanced['outcome'], 'ok', advanced)
                receipt['private'] = {'durableAssociation': 'actual-session-file'}
            return receipt
        self.execution.perform = perform
        self.assertEqual(self.controller.handle(self.start_request())['outcome'], 'accepted')
        self.drain()
        run_id = self.status()['run']['fence']['runId']
        self.controller = self.restart()
        self.assertEqual(self.store.read(self.scope)['private'][run_id]['adapter']['durableAssociation'], 'actual-session-file')
        self.assertEqual(self.execution.launches, 1)
        retained = self.store.read(self.scope)['private'][run_id]
        self.assertEqual(retained['sequence'], 2)
        self.assertEqual(self.status()['run']['execution']['state'], 'running')

    def test_recovered_launch_preserves_callback_binding_and_accepts_next_turn(self):
        accepted = self.controller.handle(self.start_request())
        self.controller.work_once()
        self.controller.work_once()
        self.execution.crash = True
        with self.assertRaises(SystemExit):
            self.controller.work_once()
        operation_id = accepted['operation']['operationId']
        observation = json.loads((self.home / operation_id).read_text())['observation']
        run_id = accepted['run']['fence']['runId']
        credential = self.store.read(self.scope)['private'][run_id]['credential']
        bound = self.call('observe', {'credential': credential, 'observation': observation})
        self.assertEqual(bound['run']['execution']['state'], 'idle')
        self.controller = self.restart()
        self.execution.crash = False
        self.drain()
        self.assertEqual(self.status()['run']['execution']['state'], 'idle')
        observation = copy.deepcopy(observation)
        observation.update(eventId='next-turn', sequence=observation['sequence'] + 1)
        observation['event'] = {'kind': 'activity', 'activity': 'active', 'turn': 'working'}
        active = self.call('observe', {'credential': credential, 'observation': observation})
        self.assertEqual(active['outcome'], 'ok', active)
        self.assertEqual(active['run']['execution']['state'], 'running')
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
        identity = self.handoff()
        git(self.repo, 'merge', '--ff-only', identity['head'])
        snapshot = self.assert_manual_handoff(self.reconcile())
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
        self.assert_handoff_revoked(current, snapshot)
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


    def test_manual_command_quotes_hostile_identity_database_checkout_and_owner(self):
        hostile_repo = self.home / "checkout '; $(printf injected); #"
        git(self.home, 'clone', str(self.repo), str(hostile_repo))
        self.repo = hostile_repo
        self.scope['repository'] = repository_identity(str(self.repo))
        self.scope['beadsDir'] = str(self.repo / ".beads '; $(printf injected); #")
        self.scope['issueId'] = "full-id'; printf injected; #"
        self.target['repository'] = self.scope['repository']
        self.workspace.root = self.repo
        self.authority.row['id'] = self.scope['issueId']
        owner = "owner'; $(printf injected); #"
        self.authority.owner = lambda scope: owner
        def claim(step, run, intent, private):
            self.authority.row.update(status='in_progress', assignee=owner)
            return self.authority.recover(step, run, intent, private)
        self.authority.perform = claim
        self.start()
        identity = self.handoff(submission_id="source'; $(printf injected); #")
        git(self.repo, 'merge', '--ff-only', identity['head'])
        run = self.reconcile()
        snapshot = self.assert_manual_handoff(run)
        self.assertEqual(snapshot['owner'], owner)
        argv = shlex.split(run['closure']['manualCommand'])
        self.assertEqual(argv, ['BEADS_DIR=' + self.scope['beadsDir'], 'bd', '--sandbox',
            '-C', self.scope['repository']['root'], 'close', '--reason', snapshot['evidenceReason'],
            '--', self.scope['issueId']])
        self.assertIn(run['fence']['runId'], snapshot['evidenceReason'])
        self.assertEqual(self.authority.row['status'], 'in_progress')
        self.assertEqual(self.authority.row['metadata'], {'unrelated': {'keep': True}})
        self.assertTrue(self.workspace.path.exists())

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
        self.assert_handoff_revoked(changed)
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

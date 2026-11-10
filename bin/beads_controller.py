"""Single local lifecycle owner. Execution, workspace and merge observations are boundaries.

Adapters implement perform(step, run, intent, private) and recover(...) returning a
validated receipt, or None when the effect cannot yet be established. Recover is
read-only: an uncertain launch is NEVER performed again. Private state is passed
by value and adapters return private changes as receipt['private']; no public
response contains it. Optional preflight/inspect hooks are read-only; inspection
refreshes exact owner activity before focus/resume, never a session discovery.
"""
import copy
import hmac
from pathlib import Path
import secrets
import shlex
import subprocess
import uuid

from beads_contract import Refusal, canonical_json, now, validate
from beads_identity import Authority, command, validate_workspace
from beads_integration import IntegrationObserver, ancestor
from beads_store import Store

CAPABILITIES = ['launch', 'exact_resume', 'durable_identity', 'owner_activity',
                'structured_handoff', 'explicit_release', 'read_only']
START_CAPABILITIES = ('launch', 'exact_resume', 'durable_identity', 'owner_activity')
METHOD_ACTION = {'start_or_focus': 'started', 'resume': 'resumed', 'release': 'release_requested',
                 'reconcile': 'reconciled', 'adopt_legacy': 'adopted'}


def key(identity):
    return {name: identity[name] for name in ('submissionId', 'submissionRevision')}


def touch(run):
    run['revision'] += 1
    run['updatedAt'] = now()


def expected(run, condition):
    if not run:
        if condition is not None:
            raise Refusal('stale_attempt', 'No current run; refresh status.')
        return
    if condition is None or condition['fence']['runId'] != run['fence']['runId'] or condition['fence']['attempt'] != run['fence']['attempt']:
        raise Refusal('stale_attempt', 'Current attempt changed; refresh status, do not automatically retry.')
    if condition['fence']['generation'] != run['fence']['generation']:
        raise Refusal('stale_generation', 'Process incarnation changed; refresh status.')
    if condition['revision'] != run['revision']:
        raise Refusal('stale_revision', 'Run changed; review current status before retrying.')


class Controller:
    def __init__(self, machine_id, store=None, authority=None, execution=None, workspace=None, integration=None):
        if not machine_id:
            raise Refusal('unknown_machine', 'A persisted local machine identity is required.')
        self.machine_id = machine_id
        self.store = store if store is not None else Store()
        self.authority = authority if authority is not None else Authority()
        self.execution = execution
        self.workspace = workspace
        self.integration = integration if integration is not None else IntegrationObserver()

    def support(self):
        supported = list(self.execution.capabilities) if self.execution else []
        return {'harnesses': ['omp'], 'execution': {'harness': 'omp', 'supported': supported,
                'unsupported': [c for c in CAPABILITIES if c not in supported]},
                'workspaceProvider': 'omp-worktree',
                'integrationModes': ['local-target', 'github-pr', 'remote-target']}

    def require(self, *capabilities):
        supported = self.support()['execution']['supported']
        for capability in capabilities:
            if capability not in supported:
                raise Refusal('unsupported_capability', f'Installed omp adapter lacks {capability}; no substitute execution is safe.', capability=capability)

    @staticmethod
    def adapter_call(method, *args):
        try:
            return method(*args)
        except (OSError, subprocess.SubprocessError) as error:
            raise Refusal('evidence_unavailable', f'Adapter IO failed: {error}; inspect exact resources before retrying.', True) from error

    def preflight(self, run):
        if self.execution and hasattr(self.execution, 'preflight'):
            self.adapter_call(self.execution.preflight, copy.deepcopy(run))

    def refresh_activity(self, run, doc):
        if self.execution and hasattr(self.execution, 'inspect'):
            private = doc['private'][run['fence']['runId']]
            activity = self.adapter_call(self.execution.inspect, copy.deepcopy(run), copy.deepcopy(private))
            if activity not in ('active', 'inactive', 'unknown'):
                raise Refusal('evidence_invalid', 'Execution inspection returned invalid owner activity.')
            run['execution'].update(activity=activity, observedAt=now())
            touch(run)

    def check_owner(self, run):
        if self.authority.owner(run['scope']) != run['owner']:
            raise Refusal('owner_mismatch', 'Only the original local owner may focus or resume this retained attempt.')

    def success(self, request, run, operation=None, action='status'):
        actions = []
        if run:
            for method in ('resume', 'handoff', 'release', 'reconcile'):
                refusal = None
                if run['disposition'] != 'current':
                    refusal = 'issue_changed'
                elif method in ('resume', 'handoff') and run['release']['state'] == 'released':
                    refusal = 'run_released'
                elif method == 'handoff' and run['execution']['access'] != 'writable':
                    refusal = 'run_read_only'
                elif method in ('resume', 'handoff', 'release') and {
                        'resume': 'exact_resume', 'handoff': 'structured_handoff',
                        'release': 'explicit_release'}[method] not in self.support()['execution']['supported']:
                    refusal = 'unsupported_capability'
                actions.append({'method': method, 'enabled': refusal is None, 'refusal': refusal})
        return {'apiVersion': 1, 'requestId': request['requestId'], 'outcome': 'ok', 'action': action,
                'run': copy.deepcopy(run), 'operation': copy.deepcopy(operation),
                'actions': actions, 'support': self.support()}

    @staticmethod
    def failure(request_id, error, run=None, operation=None):
        return {'apiVersion': 1, 'requestId': request_id, 'outcome': 'refused',
                'error': {'code': error.code, 'message': error.message,
                          'retryable': error.retryable, 'capability': error.capability},
                'run': copy.deepcopy(run), 'operation': copy.deepcopy(operation)}

    @staticmethod
    def latest(doc, run):
        if not run:
            return None
        matches = [op['public'] for op in doc['operations'].values()
                   if op['public']['fence']['runId'] == run['fence']['runId']]
        return max(matches, key=lambda op: (op['createdAt'], op['operationId']), default=None)

    def handle(self, request, trusted_human=False):
        request_id = request.get('requestId') if isinstance(request, dict) else None
        request_id = request_id if isinstance(request_id, str) and request_id else None
        run = operation = None
        try:
            if isinstance(request, dict) and request.get('apiVersion') != 1:
                raise Refusal('unsupported_version', 'Use apiVersion 1.')
            validate('controller', request, 'Request')
            scope, params, method = request['scope'], request['params'], request['method']
            self.authority.validate_scope(scope, self.machine_id)
            if method == 'status':
                doc = self.store.read(scope)
                run_id = params['runId'] or doc['current']
                if run_id and run_id not in doc['runs']:
                    raise Refusal('stale_attempt', 'Requested retained run does not exist in this scope.')
                run = doc['runs'].get(run_id)
                return self.success(request, run, self.latest(doc, run))
            with self.store.transaction(scope) as doc:
                run = doc['runs'].get(doc['current'])
                operation = self.latest(doc, run)
                payload = canonical_json(request)
                prior = doc['requests'].get(request_id)
                if prior:
                    if prior['payload'] != payload:
                        raise Refusal('request_id_conflict', 'Request ID already binds a different payload; do not reuse it.')
                    if prior['operationId']:
                        op = doc['operations'][prior['operationId']]
                        if op['result'] is not None:
                            return copy.deepcopy(op['result'])
                        return {'apiVersion': 1, 'requestId': request_id, 'outcome': 'accepted',
                                'run': copy.deepcopy(doc['runs'][op['public']['fence']['runId']]),
                                'operation': copy.deepcopy(op['public'])}
                    return copy.deepcopy(prior['result'])
                if method == 'observe':
                    if not run:
                        raise Refusal('stale_attempt', 'Callback has no current run.')
                    result = self.observe(request, doc, run)
                else:
                    expected(run, params.get('expected'))
                    if method == 'adopt_legacy':
                        result = self.adopt(request, doc, run)
                    elif method == 'start_or_focus':
                        result = self.start(request, doc, run)
                    else:
                        if not run:
                            raise Refusal('stale_attempt', 'Create a run before this operation.')
                        if method not in ('reconcile', 'release'):
                            self.guard_issue(run, writable=method == 'handoff' or (method == 'resume' and params['access'] == 'writable'))
                        if method in ('resume', 'release'):
                            if method == 'resume' and run['disposition'] != 'current':
                                raise Refusal('issue_changed', 'Revoked run cannot acquire new execution authority.')
                            if run['release']['state'] == 'released':
                                raise Refusal('run_released', 'Released runs are inspection-only; create an explicit new attempt for work.')
                            self.require('exact_resume' if method == 'resume' else 'explicit_release', 'durable_identity', 'owner_activity')
                            if method == 'release' and self.authority.owner(run['scope']) != run['owner']:
                                raise Refusal('owner_mismatch', 'Only the original local owner may explicitly relinquish this retained attempt.')
                            if run['workspace'] or method == 'resume':
                                self.check_workspace(run)
                            if method == 'resume':
                                self.check_owner(run)
                                self.preflight(run)
                                if not run['execution']['session']:
                                    raise Refusal('session_missing', 'No durable exact session exists; do not start fresh.')
                                self.refresh_activity(run, doc)
                                if run['execution']['activity'] != 'inactive':
                                    raise Refusal('session_owned', 'Prior owner is active or unknown; exact resume refused.')
                                if params['access'] == 'writable' and run['execution']['access'] != 'writable':
                                    raise Refusal('run_read_only', 'Integrated runs cannot resume writable work.')
                                if params['access'] == 'read_only':
                                    self.require('read_only')
                                    if run['coverage']['state'] != 'integrated':
                                        raise Refusal('run_read_only', 'Read-only resume requires current verified integration.')
                                self.no_pending(doc, run)
                                self.invalidate(run, doc, 'Incarnation changed; fresh integration evidence required.')
                                run['fence']['generation'] += 1
                                self.binding(doc, run)
                                run['execution']['state'] = 'starting'
                                run['execution']['access'] = params['access']
                                run['execution']['readOnlyAcknowledged'] = False
                                touch(run)
                            else:
                                self.no_pending(doc, run)
                                run['release']['state'] = 'requested'
                                touch(run)
                            result = self.enqueue(request, doc, run, [method])
                        elif method == 'handoff':
                            self.no_pending(doc, run)
                            self.handoff(request, doc, run)
                            result = self.success(request, run, action='handoff_recorded')
                        elif method == 'confirm_integration':
                            if not trusted_human:
                                raise Refusal('untrusted_receipt', 'Receipt requires explicit trusted local owner review; adapter callbacks cannot attest integration.')
                            receipt = params['receipt']
                            self.validate_receipt(run, receipt)
                            doc['private'][run['fence']['runId']].setdefault('integrationReceipts', []).append(copy.deepcopy(receipt))
                            touch(run)
                            result = self.success(request, run, action='receipt_recorded')
                        else:
                            self.no_pending(doc, run)
                            result = self.enqueue(request, doc, run, ['reconcile'])
                doc['requests'][request_id] = {'payload': payload,
                    'operationId': result['operation']['operationId'] if result.get('outcome') == 'accepted' else None,
                    'result': None if result['outcome'] == 'accepted' else copy.deepcopy(result)}
                return result
        except Refusal as error:
            return self.failure(request_id, error, run, operation)
        except (OSError, ValueError, KeyError) as error:
            return {'apiVersion': 1, 'requestId': request_id, 'outcome': 'error',
                    'error': {'code': 'io_error', 'message': str(error), 'retryable': True},
                    'run': copy.deepcopy(run), 'operation': copy.deepcopy(operation)}

    def guard_issue(self, run, writable=False):
        issue = self.authority.issue(run['scope'])
        if issue.get('assignee') != run['owner']:
            raise Refusal('owner_mismatch', 'Fresh Beads owner differs from this run.')
        if writable:
            if run['disposition'] != 'current':
                raise Refusal('issue_changed', 'Run was revoked; explicit new attempt required.')
            if issue.get('status') != 'in_progress':
                raise Refusal('issue_changed', 'Writable actions require the self-owned in-progress Bead.')
            self.authority.eligible(run['scope'], run['owner'])
        elif issue.get('status') not in ('in_progress', 'closed'):
            raise Refusal('issue_changed', 'Bead was reopened, deferred or cancelled; inspect before acting.')
        return issue

    def check_workspace(self, run):
        if not run['workspace']:
            raise Refusal('workspace_missing', 'No acknowledged workspace exists.')
        validate_workspace(run['workspace'], run['scope'])
        if not self.workspace:
            raise Refusal('unsupported_capability', 'Workspace provider identity/registry validation unavailable.')
        self.adapter_call(self.workspace.validate, copy.deepcopy(run))

    @staticmethod
    def no_pending(doc, run):
        if any(op['public']['fence']['runId'] == run['fence']['runId'] and op['public']['state'] in ('pending', 'running') for op in doc['operations'].values()):
            raise Refusal('pending_operation', 'A durable operation is pending; inspect/reconcile it instead of issuing another.', True)

    @staticmethod
    def binding(doc, run):
        private = doc['private'].setdefault(run['fence']['runId'], {})
        private.update({'bindingId': str(uuid.uuid4()), 'credential': secrets.token_urlsafe(32), 'sequence': 0, 'events': {}})

    @staticmethod
    def new_run(scope, owner, attempt, target, policy):
        timestamp = now()
        return {'schemaVersion': 1, 'fence': {'runId': str(uuid.uuid4()), 'attempt': attempt, 'generation': 1},
                'revision': 1, 'scope': copy.deepcopy(scope), 'owner': owner, 'disposition': 'current',
                'harness': 'omp', 'policy': copy.deepcopy(policy), 'target': copy.deepcopy(target),
                'workspace': None, 'execution': {'state': 'preparing', 'access': 'writable', 'activity': 'unknown',
                'session': None, 'terminal': None, 'observedAt': None, 'readOnlyAcknowledged': False},
                'coverage': {'revision': 0, 'workspaceHead': None, 'required': [], 'state': 'unsubmitted'},
                'submissions': [], 'release': {'state': 'held', 'receipt': None},
                'closure': {'state': 'not_requested', 'evidenceIds': [], 'observedAt': None,
                            'reason': 'Integration destination unbound — explicit handoff required.' if target is None else None,
                            'manualCommand': None},
                'cleanup': {'state': 'held', 'blockers': ['manual_cleanup_required'], 'checkedAt': None},
                'issueObservation': None, 'createdAt': timestamp, 'updatedAt': timestamp}

    def adopt(self, request, doc, run):
        self.require(*START_CAPABILITIES)
        if not self.execution or not hasattr(self.execution, 'adopt') or not self.workspace or not hasattr(self.workspace, 'adopt'):
            raise Refusal('unsupported_capability', 'Exact legacy execution/workspace adoption is unavailable.')
        if run and run['execution']['session']:
            # Repeated adoption uses the same exact focus/resume path, never a new attempt.
            return self.start(request, doc, run, params={
                'harness': run['harness'], 'target': run['target'],
                'policy': run['policy'], 'newAttempt': False})
        if run:
            self.no_pending(doc, run)
            self.check_owner(run)
            self.guard_issue(run, writable=True)
            if not doc['private'][run['fence']['runId']].get('adoption'):
                raise Refusal('session_missing', 'This reservation is not a legacy adoption; no identity substitution.')
        else:
            owner = self.authority.owner(request['scope'])
            issue = self.authority.eligible(request['scope'], owner)
            if issue.get('status') != 'in_progress' or issue.get('assignee') != owner:
                raise Refusal('owner_mismatch', 'Adoption requires the already self-owned in-progress Bead; no claim is made.')
            run = self.new_run(request['scope'], owner, 1, None,
                               {'closeAfterIntegration': False, 'cleanupAfterRelease': False})
            doc['current'] = run['fence']['runId']
            doc['runs'][doc['current']] = run
            doc['private'][doc['current']] = {'adoption': True}
            self.binding(doc, run)
        doc['audit'].append({'kind': 'adoption_intent', 'fence': copy.deepcopy(run['fence']), 'at': now()})
        return self.enqueue(request, doc, run, ['adopt'])

    def start(self, request, doc, run, params=None):
        params = request['params'] if params is None else params
        if params['harness'] != 'omp':
            raise Refusal('unsupported_harness', 'Only the concrete omp execution adapter is supported.')
        if params['target'] is not None:
            self.target(params['target'], request['scope'])
        if run and not params['newAttempt']:
            if run['target'] != params['target'] or run['policy'] != params['policy']:
                raise Refusal('target_changed', 'Current target/policy differs; declare a writable handoff or explicit new attempt.')
            self.no_pending(doc, run)
            if run['release']['state'] == 'released':
                raise Refusal('run_released', 'Released run retained for inspection; new attempt must be explicit.')
            self.check_owner(run)
            self.guard_issue(run, writable=run['execution']['access'] == 'writable')
            self.require(*START_CAPABILITIES)
            self.check_workspace(run)
            self.preflight(run)
            self.refresh_activity(run, doc)
            if run['execution']['activity'] == 'active':
                return self.enqueue(request, doc, run, ['focus'])
            if run['execution']['activity'] != 'inactive':
                raise Refusal('session_owned', 'Owner activity is unknown; inspect the existing incarnation before resuming.')
            if not run['execution']['session']:
                raise Refusal('session_missing', 'No exact durable session; release the failed reservation, never start a fallback session.')
            if run['execution']['access'] != 'writable':
                raise Refusal('run_read_only', 'Start cannot implicitly launch a read-only replacement; use explicit read-only resume.')
            self.invalidate(run, doc, 'Exact resume starts a new incarnation; fresh evidence required.')
            run['fence']['generation'] += 1
            self.binding(doc, run)
            run['execution'].update(state='starting', readOnlyAcknowledged=False)
            touch(run)
            return self.enqueue(request, doc, run, ['resume'])
        if params['target'] is None:
            raise Refusal('target_changed', 'New runs require an explicit integration destination; use adopt_legacy for existing history.')
        self.require(*START_CAPABILITIES)
        if not self.workspace:
            raise Refusal('unsupported_capability', 'omp-worktree provider is not installed.')
        if run and run['release']['state'] != 'released':
            raise Refusal('session_owned', 'A new attempt requires explicit acknowledged prior-owner release.')
        owner = self.authority.owner(request['scope'])
        if self.authority.issue(request['scope']).get('issue_type') == 'epic':
            raise Refusal('issue_not_ready', 'Epics are sessionless containers; choose a concrete child.')
        self.authority.eligible(request['scope'], owner)
        attempt = max((r['fence']['attempt'] for r in doc['runs'].values()), default=0) + 1
        previous = run
        run = self.new_run(request['scope'], owner, attempt, params['target'], params['policy'])
        self.preflight(run)
        # Resource allocation is planned without effects and journaled before perform.
        if not hasattr(self.workspace, 'plan'):
            raise Refusal('unsupported_capability', 'Workspace provider lacks durable resource planning.')
        planned = self.adapter_call(self.workspace.plan, copy.deepcopy(run))
        validate('run', planned, 'Workspace')
        if planned['sourceRepository'] != run['scope']['repository'] or str(Path(planned['path']).resolve()) != planned['path']:
            raise Refusal('workspace_mismatch', 'Planned workspace must bind canonical source and path.')
        if any(old['workspace'] and old['workspace']['path'] == planned['path'] for old in doc['runs'].values()):
            raise Refusal('workspace_mismatch', 'Retained attempt workspace cannot be reused by a new attempt.')
        if previous:
            self.no_pending(doc, previous)
            self.invalidate(previous, doc, 'Superseded attempt; prior integration evidence is retained for audit only.')
            previous['disposition'] = 'superseded'
            touch(previous)
        doc['current'] = run['fence']['runId']
        doc['runs'][doc['current']] = run
        doc['private'][doc['current']] = {'plannedWorkspace': copy.deepcopy(planned)}
        self.binding(doc, run)
        return self.enqueue(request, doc, run, ['claim', 'workspace', 'launch'])

    @staticmethod
    def target(target, scope):
        if target['mode'] != 'github-pr' and target['repository'] != scope['repository']:
            raise Refusal('repository_mismatch', 'Local/remote target must name the canonical source repository, not a clone.')
        command(['git', 'check-ref-format', target['ref']], scope['repository']['root'])

    @staticmethod
    def enqueue(request, doc, run, steps):
        timestamp = now()
        public = {'operationId': str(uuid.uuid4()), 'requestId': request['requestId'], 'method': request['method'],
                  'fence': copy.deepcopy(run['fence']), 'state': 'pending', 'phase': 'reserved',
                  'createdAt': timestamp, 'updatedAt': timestamp, 'error': None}
        doc['operations'][public['operationId']] = {'public': public, 'request': copy.deepcopy(request),
            'result': None, 'intent': {'steps': steps, 'index': 0, 'started': False}, 'receipts': []}
        return {'apiVersion': 1, 'requestId': request['requestId'], 'outcome': 'accepted',
                'run': copy.deepcopy(run), 'operation': copy.deepcopy(public)}

    def handoff(self, request, doc, run):
        params = request['params']
        if run['execution']['access'] != 'writable' or run['release']['state'] != 'held':
            raise Refusal('run_read_only', 'Handoff cannot resurrect integrated or released execution.')
        self.require('structured_handoff')
        self.check_workspace(run)
        if params['previousCoverageRevision'] != run['coverage']['revision']:
            raise Refusal('submission_changed', 'Coverage changed; review the complete required set.')
        head = command(['git', 'rev-parse', 'HEAD'], run['workspace']['path'])
        if head != params['workspaceHead']:
            raise Refusal('submission_changed', 'Handoff workspace HEAD no longer matches.')
        self.target(params['target'], run['scope'])
        identities = params['requiredSubmissions']
        if len({canonical_json(key(i)) for i in identities}) != len(identities):
            raise Refusal('submission_changed', 'Required submission keys must be unique.')
        if not any(identity['head'] == head for identity in identities):
            raise Refusal('coverage_incomplete', 'Required submissions must include the exact workspace HEAD, not only earlier work.')
        if not all(ancestor(run['workspace']['path'], identity['head'], head) for identity in identities):
            raise Refusal('coverage_incomplete', 'Every required source must be present in the declared workspace history.')
        doc['audit'].append({'kind': 'handoff', 'fence': copy.deepcopy(run['fence']),
                             'coverage': copy.deepcopy(run['coverage']), 'submissions': copy.deepcopy(run['submissions']),
                             'request': copy.deepcopy(request), 'at': now()})
        for identity in identities:
            if identity['target'] != params['target']:
                raise Refusal('target_changed', 'Every required submission must bind the explicit current mode/target.')
            prior = [s for s in run['submissions'] if s['identity']['submissionId'] == identity['submissionId']]
            same = next((s for s in prior if key(s['identity']) == key(identity)), None)
            if same and same['identity'] != identity:
                raise Refusal('submission_changed', 'Submission identities are immutable; increase submissionRevision.')
            if not same and prior and identity['submissionRevision'] <= max(s['identity']['submissionRevision'] for s in prior):
                raise Refusal('submission_changed', 'Replacement submission must increase its revision.')
            if not same:
                run['submissions'].append({'identity': copy.deepcopy(identity), 'state': 'pending', 'observedAt': None, 'evidence': None})
        for submission in run['submissions']:
            if key(submission['identity']) in [key(i) for i in identities]:
                submission.update(state='pending', observedAt=None, evidence=None)
        run['target'] = copy.deepcopy(params['target'])
        run['coverage'] = {'revision': run['coverage']['revision'] + 1, 'workspaceHead': head,
                           'required': [key(i) for i in identities], 'state': 'pending'}
        run['closure'].update(state='not_requested', evidenceIds=[], reason=None, manualCommand=None)
        touch(run)

    @staticmethod
    def validate_receipt(run, receipt):
        if receipt['fence'] != run['fence'] or receipt['target'] != run['target']:
            raise Refusal('evidence_invalid', 'Receipt fence or target differs from the current run.')
        matches = [s for s in run['submissions'] if key(s['identity']) == receipt['submission'] and key(s['identity']) in run['coverage']['required']]
        if len(matches) != 1 or matches[0]['identity']['head'] != receipt['head']:
            raise Refusal('submission_changed', 'Receipt must map an exact current required submission and SHA.')
        if receipt['attestedBy'] != run['owner']:
            raise Refusal('untrusted_receipt', 'Receipt must be explicitly attested by the current owner.')

    def observe(self, request, doc, run):
        params = request['params']
        observation = params['observation']
        private = doc['private'][run['fence']['runId']]
        if observation['fence'] != run['fence']:
            raise Refusal('stale_generation', 'Observation belongs to another attempt/incarnation.')
        if observation['bindingId'] != private['bindingId'] or not hmac.compare_digest(params['credential'], private['credential']):
            raise Refusal('session_mismatch', 'Callback credential/binding does not match this incarnation.')
        event_payload = canonical_json(observation)
        if observation['eventId'] in private['events']:
            if private['events'][observation['eventId']] != event_payload:
                raise Refusal('request_id_conflict', 'Event ID reused for another observation.')
            return self.success(request, run, action='duplicate')
        if observation['sequence'] <= private['sequence']:
            raise Refusal('stale_generation', 'Out-of-order adapter observation; no state changed.')
        if run['disposition'] != 'current':
            raise Refusal('issue_changed', 'Revoked run cannot regain authority through callbacks.')
        self.guard_issue(run)
        self.check_workspace(run)
        if not self.execution:
            raise Refusal('unsupported_capability', 'Observation requires a validating execution adapter.')
        # Verification is read-only; callback shape/secret alone never proves ownership.
        self.adapter_call(self.execution.validate_observation, copy.deepcopy(run), copy.deepcopy(observation), copy.deepcopy(private))
        self.apply_event(run, observation, private, doc)
        if hasattr(self.execution, 'publish_observation'):
            self.adapter_call(self.execution.publish_observation, copy.deepcopy(run), copy.deepcopy(observation), copy.deepcopy(private))
        private['events'][observation['eventId']] = event_payload
        private['sequence'] = observation['sequence']
        touch(run)
        return self.success(request, run, action='observation_recorded')

    @staticmethod
    def apply_event(run, observation, private, doc):
        event = observation['event']
        kind = event['kind']
        execution = run['execution']
        session = execution['session']
        if run['release']['state'] == 'released':
            raise Refusal('run_released', 'Released incarnation cannot emit new execution transitions.')
        if kind == 'session_bound':
            if execution['state'] != 'starting' or session is not None and session['sessionId'] != event['session']['sessionId']:
                raise Refusal('session_mismatch', 'Session binding requires pending launch/resume of the exact conversation.')
            if event['session']['bindingId'] != private['bindingId']:
                raise Refusal('session_mismatch', 'Acknowledged session has another binding.')
            if not event['terminal'] or event['terminal']['machineId'] != run['scope']['machineId']:
                raise Refusal('terminal_mismatch', 'Acknowledged session needs its exact local terminal.')
            execution.update(session=copy.deepcopy(event['session']), terminal=copy.deepcopy(event['terminal']),
                             activity=event['activity'], state='idle')
        elif kind == 'primary_session_changed':
            if session != event['previousSession'] or event['session']['bindingId'] != private['bindingId']:
                raise Refusal('session_mismatch', 'Primary-session change must replace the exact current binding.')
            execution['session'] = copy.deepcopy(event['session'])
        elif kind == 'activity':
            if not session or execution['state'] not in ('running', 'idle'):
                raise Refusal('session_missing', 'Activity cannot create or revive an unbound/stopped session.')
            execution.update(activity=event['activity'], state='running' if event['turn'] == 'working' else 'idle')
        elif kind == 'exited':
            execution.update(activity=event['activity'], state='stopped' if event['activity'] == 'inactive' else execution['state'])
        elif kind == 'read_only_acknowledged':
            if execution['access'] != 'read_only' or event['session'] != session:
                raise Refusal('session_mismatch', 'Read-only acknowledgement was not requested for this session.')
            execution['readOnlyAcknowledged'] = True
        elif kind == 'release_acknowledged':
            receipt = event['receipt']
            if run['release']['state'] != 'requested' or receipt['fence'] != run['fence'] or receipt['session'] != session:
                raise Refusal('session_mismatch', 'Release requires a preceding owner request and exact session/fence.')
            run['release'] = {'state': 'released', 'receipt': copy.deepcopy(receipt)}
            execution.update(state='released', access='read_only', activity='inactive')
        execution['observedAt'] = observation['observedAt']

    def reconcile_runs(self):
        """Reobserve current runs independently of pending execution effects.

        Reads have no external effect to replay. Commit only semantic changes,
        fenced against concurrent handoffs/callbacks; unchanged polls update
        service health rather than accumulating unbounded operation receipts.
        """
        def stable(value):
            if isinstance(value, dict):
                return {k: stable(v) for k, v in value.items() if k not in ('observedAt', 'evidenceId')}
            if isinstance(value, list):
                return [stable(v) for v in value]
            return value

        def fingerprint(run, receipt, error):
            return canonical_json(stable({'fence': run['fence'], 'disposition': run['disposition'],
                'coverageRevision': run['coverage']['revision'], 'coverageState': run['coverage']['state'],
                'closureState': run['closure']['state'], 'access': run['execution']['access'],
                'capabilities': self.support()['execution']['supported'], 'receipt': receipt, 'error': error}))

        errors = []
        try:
            with self.store.worker_lock():
                for scope in self.store.documents():
                    if scope['machineId'] != self.machine_id:
                        continue
                    doc = self.store.read(scope)
                    snapshot = doc['runs'].get(doc['current'])
                    if not snapshot:
                        continue
                    run_id = snapshot['fence']['runId']
                    private = doc['private'][run_id]
                    receipt = error = None
                    try:
                        self.authority.validate_scope(scope, self.machine_id)
                        receipt = self.reconcile(copy.deepcopy(snapshot), copy.deepcopy(private))
                        error = receipt.get('error')
                    except Refusal as refusal:
                        error = {'code': refusal.code, 'message': refusal.message}
                    if error:
                        errors.append({'scope': copy.deepcopy(scope), **error})
                    with self.store.transaction(scope) as doc:
                        run = doc['runs'].get(doc['current'])
                        if not run or run['fence'] != snapshot['fence'] or run['revision'] != snapshot['revision']:
                            continue
                        private = doc['private'][run_id]
                        if private.get('serviceObservation') == fingerprint(snapshot, receipt, error):
                            if not error and private.get('serviceError'):
                                errors.append({'scope': copy.deepcopy(scope), **private['serviceError']})
                            continue
                        private['serviceError'] = None
                        if receipt is None:
                            self.invalidate(run, doc, error['message'])
                        else:
                            try:
                                self.apply_reconcile(run, receipt, doc)
                            except Refusal as refusal:
                                self.invalidate(run, doc, refusal.message)
                                private['serviceError'] = {'code': refusal.code, 'message': refusal.message}
                                errors.append({'scope': copy.deepcopy(scope), **private['serviceError']})
                        private['serviceObservation'] = fingerprint(run, receipt, error)
                        if (run['execution']['access'] == 'read_only'
                                and not run['execution']['readOnlyAcknowledged']
                                and run['release']['state'] != 'released' and run['execution']['session']):
                            if 'read_only' in self.support()['execution']['supported']:
                                pending = any(op['public']['fence'] == run['fence']
                                    and op['public']['state'] in ('pending', 'running')
                                    and 'read_only' in op['intent']['steps'] for op in doc['operations'].values())
                                if not pending:
                                    self.enforce_revocation(doc, run)
                            elif 'capability_unsupported' not in run['cleanup']['blockers']:
                                run['cleanup']['blockers'].append('capability_unsupported')
                        touch(run)
            return errors
        except Refusal as error:
            if error.code == 'pending_operation':
                return [{'code': error.code, 'message': error.message}]
            raise

    def work_once(self):
        # This retained lock protects external IO, not short request transactions.
        try:
            with self.store.worker_lock():
                for scope in self.store.documents():
                    if scope['machineId'] != self.machine_id:
                        continue
                    doc = self.store.read(scope)
                    for operation_id, operation in doc['operations'].items():
                        if operation['public']['state'] in ('pending', 'running') and self.step(scope, operation_id):
                            return True
                return False
        except Refusal as error:
            if error.code == 'pending_operation':
                return False
            raise

    def step(self, scope, operation_id):
        with self.store.transaction(scope) as doc:
            operation = doc['operations'][operation_id]
            public, intent = operation['public'], operation['intent']
            run = doc['runs'][public['fence']['runId']]
            if doc['current'] != run['fence']['runId'] or run['fence'] != public['fence']:
                self.finish(doc, operation, run, Refusal('stale_generation', 'Discarded pending operation for a replaced incarnation.'))
                return True
            recovering = intent['started']
            step = intent['steps'][intent['index']]
            intent['started'] = True
            public.update(state='running', phase={'claim': 'reserved', 'workspace': 'workspace', 'launch': 'launch',
                          'resume': 'launch', 'focus': 'inspecting', 'release': 'releasing',
                          'read_only': 'binding', 'reconcile': 'verifying', 'adopt': 'inspecting'}[step], updatedAt=now())
            if step in ('launch', 'resume'):
                run['execution']['state'] = 'starting'
                touch(run)
            snapshot = copy.deepcopy(run)
            private = copy.deepcopy(doc['private'][run['fence']['runId']])
            saved_intent = copy.deepcopy(intent)
        try:
            self.authority.validate_scope(scope, self.machine_id)
            if step not in ('claim', 'reconcile', 'release', 'read_only'):
                self.guard_issue(snapshot, writable=step in ('workspace', 'launch') or (step in ('resume', 'focus') and snapshot['execution']['access'] == 'writable'))
            if not recovering and step in ('claim', 'workspace', 'launch', 'resume', 'focus'):
                self.check_owner(snapshot)
                self.preflight(snapshot)
            if snapshot['workspace'] and step != 'reconcile':
                self.check_workspace(snapshot)
            if step == 'adopt':
                receipt = self.adapter_call(self.execution.adopt, snapshot,
                           {'operationId': operation_id, 'fence': public['fence'], **saved_intent}, private)
                if receipt is not None:
                    receipt['workspace'] = self.adapter_call(self.workspace.adopt, snapshot, receipt['workspacePath'])
            elif step == 'reconcile':
                receipt = self.reconcile(snapshot, private)
            else:
                adapter = self.authority if step == 'claim' else self.workspace if step == 'workspace' else self.execution
                if adapter is None:
                    raise Refusal('unsupported_capability', f'No concrete adapter for {step}.')
                receipt = self.adapter_call(adapter.recover if recovering else adapter.perform, step, snapshot,
                           {'operationId': operation_id, 'fence': public['fence'], **saved_intent}, private)
            with self.store.transaction(scope) as doc:
                operation = doc['operations'][operation_id]
                run = doc['runs'][operation['public']['fence']['runId']]
                if run['fence'] != snapshot['fence'] or doc['current'] != run['fence']['runId']:
                    self.finish(doc, operation, run, Refusal('stale_generation', 'External observation discarded after incarnation changed.'))
                    return True
                if receipt is None:
                    operation['public']['error'] = {'code': 'pending_operation', 'message': f'{step} outcome unconfirmed; inspect exact resources. Recovery will observe, never relaunch.'}
                    return False
                self.apply_receipt(step, receipt, run, doc)
                if step == 'reconcile' and run['execution']['access'] == 'read_only' and not run['execution']['readOnlyAcknowledged'] and run['release']['state'] != 'released' and run['execution']['session']:
                    if 'read_only' in self.support()['execution']['supported']:
                        if run['fence'] == operation['public']['fence']:
                            operation['intent']['steps'].append('read_only')
                        else:
                            self.enforce_revocation(doc, run)
                    else:
                        run['cleanup']['blockers'].append('capability_unsupported')
                operation['receipts'].append({'step': step, 'receipt': copy.deepcopy(receipt), 'observedAt': now()})
                if step == 'adopt' and run['execution']['activity'] == 'active':
                    operation['intent']['steps'].append('focus')
                operation['intent']['index'] += 1
                operation['intent']['started'] = False
                operation['public'].update(updatedAt=now(), error=None)
                touch(run)
                if operation['intent']['index'] == len(operation['intent']['steps']):
                    self.finish(doc, operation, run)
            return True
        except (Refusal, OSError) as error:
            if isinstance(error, OSError):
                error = Refusal('evidence_unavailable', f'Worker IO failed: {error}; inspect exact resources before retrying.', True)
            with self.store.transaction(scope) as doc:
                operation = doc['operations'][operation_id]
                run = doc['runs'][operation['public']['fence']['runId']]
                if step == 'reconcile':
                    self.invalidate(run, doc, error.message)
                    touch(run)
                # Indeterminate effects stay recoverable. Never call perform again.
                if error.retryable:
                    operation['public']['error'] = {'code': error.code, 'message': error.message}
                    operation['public']['updatedAt'] = now()
                else:
                    self.finish(doc, operation, run, error)
            return False

    def apply_receipt(self, step, receipt, run, doc):
        if step == 'adopt':
            workspace = receipt['workspace']
            validate_workspace(workspace, run['scope'])
            self.adapter_call(self.workspace.validate, {**copy.deepcopy(run), 'workspace': workspace})
            session, terminal, activity = receipt['session'], receipt['terminal'], receipt['activity']
            validate('run', session, 'Session')
            private = doc['private'][run['fence']['runId']]
            if (session['harness'] != run['harness'] or session['bindingId'] != private['bindingId']
                    or activity not in ('active', 'inactive')):
                raise Refusal('session_mismatch', 'Adoption did not establish exact execution identity.')
            if terminal is not None:
                validate('run', terminal, 'Terminal')
                if terminal['machineId'] != run['scope']['machineId']:
                    raise Refusal('remote_machine', 'Adopted terminal belongs to another machine.')
            if activity == 'active' and terminal is None:
                raise Refusal('terminal_mismatch', 'Live adoption requires an exact local terminal.')
            run['workspace'] = copy.deepcopy(workspace)
            run['execution'].update(session=copy.deepcopy(session), terminal=copy.deepcopy(terminal),
                                    activity=activity, state='idle' if activity == 'active' else 'stopped', observedAt=now())
            doc['audit'].append({'kind': 'adoption_result', 'fence': copy.deepcopy(run['fence']),
                                 'workspace': copy.deepcopy(workspace), 'session': copy.deepcopy(session), 'at': now()})
        elif step == 'claim':
            validate('run', receipt, 'IssueObservation')
            if receipt['status'] != 'in_progress' or receipt['assignee'] != run['owner']:
                raise Refusal('owner_mismatch', 'Observed claim does not belong to this run owner.')
            run['issueObservation'] = copy.deepcopy(receipt)
        elif step == 'workspace':
            workspace = receipt['workspace']
            if workspace != doc['private'][run['fence']['runId']]['plannedWorkspace']:
                raise Refusal('workspace_mismatch', 'Workspace receipt differs from its journaled resource plan.')
            validate_workspace(workspace, run['scope'])
            self.adapter_call(self.workspace.validate, {**copy.deepcopy(run), 'workspace': workspace})
            run['workspace'] = copy.deepcopy(workspace)
        elif step == 'reconcile':
            self.apply_reconcile(run, receipt, doc)
        elif step in ('launch', 'resume', 'release', 'read_only'):
            observation = receipt['observation']
            validate('adapter', observation, 'Observation')
            private = doc['private'][run['fence']['runId']]
            if observation['fence'] != run['fence'] or observation['bindingId'] != private['bindingId']:
                raise Refusal('session_mismatch', 'Effect receipt belongs to another incarnation.')
            previous = private['events'].get(observation['eventId'])
            if previous != canonical_json(observation):
                if previous is not None or observation['sequence'] <= private['sequence']:
                    raise Refusal('stale_generation', 'Effect receipt sequence/event was already superseded.')
                self.adapter_call(self.execution.validate_observation, copy.deepcopy(run), observation, copy.deepcopy(private))
                self.apply_event(run, observation, private, doc)
                if hasattr(self.execution, 'publish_observation'):
                    self.adapter_call(self.execution.publish_observation, copy.deepcopy(run), copy.deepcopy(observation), copy.deepcopy(private))
            if step in ('launch', 'resume') and observation['event']['kind'] != 'session_bound':
                raise Refusal('session_mismatch', 'Launch needs a validated durable session-bound receipt.')
            if step == 'release' and observation['event']['kind'] != 'release_acknowledged':
                raise Refusal('session_mismatch', 'Release needs an explicit validated release receipt.')
            if step == 'read_only' and observation['event']['kind'] != 'read_only_acknowledged':
                raise Refusal('session_mismatch', 'Read-only enforcement requires an explicit adapter acknowledgement.')
            private['sequence'] = max(private['sequence'], observation['sequence'])
            private['events'][observation['eventId']] = canonical_json(observation)
        if 'private' in receipt:
            doc['private'][run['fence']['runId']].setdefault('adapter', {}).update(copy.deepcopy(receipt['private']))

    def finish(self, doc, operation, run, error=None):
        public = operation['public']
        if error is None:
            observation_error = next((entry['receipt']['error'] for entry in operation['receipts']
                                      if entry['receipt'].get('error')), None)
            if observation_error:
                error = Refusal(observation_error['code'], observation_error['message'])
        public.update(state='failed' if error else 'succeeded', phase='complete', updatedAt=now(),
                      error={'code': error.code, 'message': error.message} if error else None)
        if error:
            if public['method'] in ('start_or_focus', 'resume', 'adopt_legacy') and run['execution']['state'] in ('preparing', 'starting'):
                run['execution'].update(state='failed', activity='unknown')
                touch(run)
            result = self.failure(public['requestId'], error, run, public)
        else:
            action = ('focused' if operation['intent']['steps'] == ['focus'] else
                      'resumed' if operation['intent']['steps'] == ['resume'] else METHOD_ACTION[public['method']])
            result = self.success(operation['request'], run, public, action)
        operation['result'] = result

    def enforce_revocation(self, doc, run):
        request = {'apiVersion': 1, 'requestId': 'revoke-' + str(uuid.uuid4()), 'method': 'reconcile',
                   'scope': copy.deepcopy(run['scope']),
                   'params': {'expected': {'fence': copy.deepcopy(run['fence']), 'revision': run['revision']}}}
        result = self.enqueue(request, doc, run, ['read_only'])
        doc['requests'][request['requestId']] = {'payload': canonical_json(request),
            'operationId': result['operation']['operationId'], 'result': None}

    def reconcile(self, run, private):
        issue = self.authority.issue(run['scope'])
        result = {'issue': {'status': issue['status'], 'assignee': issue.get('assignee') or None, 'observedAt': now()},
                  'submissions': [], 'head': None, 'clean': False}
        if issue.get('assignee') != run['owner'] or issue['status'] not in ('in_progress', 'closed'):
            return result
        if run['coverage']['required']:
            # Merge observation needs recorded Git identity, not a live omp
            # process or workspace provisioning/registry capability. No cleanup
            # authority is derived from this read-only check.
            if not run['workspace']:
                raise Refusal('workspace_missing', 'No recorded submission workspace exists.')
            validate_workspace(run['workspace'], run['scope'])
            path = run['workspace']['path']
            if not self.integration:
                result['error'] = {'code': 'evidence_unavailable', 'message': 'No authoritative integration observer installed; prior proof is audit-only.'}
            else:
                try:
                    result['submissions'] = self.integration.observe(copy.deepcopy(run), copy.deepcopy(private.get('integrationReceipts', [])))
                except Refusal as error:
                    result['error'] = {'code': error.code, 'message': error.message}
            # Provider IO may be slow: sample coverage after it, not only before it.
            validate_workspace(run['workspace'], run['scope'])
            result['head'] = command(['git', 'rev-parse', 'HEAD'], path)
            result['clean'] = not command(['git', 'status', '--porcelain', '--untracked-files=all'], path)
            issue = self.authority.issue(run['scope'])
            result['issue'] = {'status': issue['status'], 'assignee': issue.get('assignee') or None, 'observedAt': now()}
        return result

    def apply_reconcile(self, run, receipt, doc):
        issue = receipt['issue']
        validate('run', issue, 'IssueObservation')
        previous = run['issueObservation']
        run['issueObservation'] = copy.deepcopy(issue)
        changed = issue['assignee'] != run['owner'] or issue['status'] not in ('in_progress', 'closed') or (previous and previous['status'] == 'closed' and issue['status'] != 'closed')
        if changed and run['disposition'] == 'current':
            run['disposition'] = 'reassigned' if issue['assignee'] != run['owner'] else 'reopened' if previous and previous['status'] == 'closed' else 'cancelled'
            run['fence']['generation'] += 1
            run['execution']['access'] = 'read_only'
            run['execution']['readOnlyAcknowledged'] = False
            run['execution']['activity'] = 'unknown'
        required = run['coverage']['required']
        doc['audit'].append({'kind': 'reconcile', 'fence': copy.deepcopy(run['fence']),
                             'submissions': copy.deepcopy(run['submissions']), 'closure': copy.deepcopy(run['closure']), 'at': now()})
        by_key = {canonical_json(key(s['identity'])): s for s in run['submissions']}
        # An issue change revokes the observation's fence; discard its proof,
        # rather than rolling back revocation while validating old-fence evidence.
        observed = [] if changed else receipt.get('submissions', [])
        if len({canonical_json(key(s['identity'])) for s in observed}) != len(observed):
            raise Refusal('evidence_invalid', 'Observer returned duplicate submission observations.')
        for submission in observed:
            validate('run', submission, 'Submission')
            identity = submission['identity']
            original = by_key.get(canonical_json(key(identity)))
            if not original or original['identity'] != identity or key(identity) not in required or identity['target'] != run['target']:
                raise Refusal('evidence_invalid', 'Observer changed a required immutable identity or mode/target.')
            evidence = submission['evidence']
            if evidence:
                if evidence['fence'] != run['fence'] or evidence['submission'] != key(identity) or evidence['head'] != identity['head'] or evidence['target'] != identity['target'] or evidence['kind'] != identity['target']['mode']:
                    raise Refusal('evidence_invalid', 'Evidence does not bind this exact fence/submission/SHA/mode/target.')
                if evidence['kind'] == 'github-pr' and (evidence['pullRequest'] != identity['pullRequest'] or evidence['mergedHead'] != identity['head']):
                    raise Refusal('evidence_invalid', 'PR proof is for a different pull request or merged head.')
        # A failed or partial fresh observation invalidates consumable cached proof.
        for submission in run['submissions']:
            if key(submission['identity']) in required:
                submission.update(state='unknown', evidence=None, observedAt=now())
        if not changed and not receipt.get('error'):
            for submission in observed:
                by_key[canonical_json(key(submission['identity']))].update(copy.deepcopy(submission))
        complete = bool(required) and not changed and run['disposition'] == 'current' and receipt.get('clean') and receipt.get('head') == run['coverage']['workspaceHead'] and all(
            by_key[canonical_json(k)]['state'] == 'integrated' and by_key[canonical_json(k)]['evidence'] for k in required)
        run['coverage']['state'] = 'integrated' if complete else 'unknown' if required else 'unsubmitted'
        run['closure'].update(state='held' if required else 'not_requested', evidenceIds=[], manualCommand=None,
                              observedAt=issue['observedAt'], reason=receipt['error']['message'] if receipt.get('error') else 'Integration proof unavailable or invalidated.' if required else
                              'Integration destination unbound — explicit handoff required.' if run['target'] is None else None)
        if complete:
            run['execution']['access'] = 'read_only'
            evidence_ids = [by_key[canonical_json(k)]['evidence']['evidenceId'] for k in required]
            reason = f"Verified {run['target']['mode']} integration; run {run['fence']['runId']} attempt {run['fence']['attempt']}; evidence {','.join(evidence_ids)}. Owner review required before manual closure."
            # Copyable normal Beads command. Display only; no execution method exists.
            close = 'BEADS_DIR=' + shlex.quote(run['scope']['beadsDir']) + ' ' + shlex.join([
                'bd', '--sandbox', '-C', run['scope']['repository']['root'],
                'close', '--reason', reason, '--', run['scope']['issueId']])
            run['closure'].update(state='observed_closed' if issue['status'] == 'closed' else 'awaiting_manual', evidenceIds=evidence_ids,
                reason='Externally closed — observed only; closure ownership is not established.' if issue['status'] == 'closed' else 'Merged — awaiting manual closure',
                manualCommand=None if issue['status'] == 'closed' else close)
        elif issue['status'] == 'closed':
            run['closure'].update(state='observed_closed', reason='Externally closed — observed only; no run closure ownership or integration inferred.')
        run['cleanup'] = {'state': 'held', 'blockers': ['manual_cleanup_required'], 'checkedAt': now()}

    @staticmethod
    def invalidate(run, doc, reason):
        doc['audit'].append({'kind': 'invalidated', 'fence': copy.deepcopy(run['fence']),
                             'submissions': copy.deepcopy(run['submissions']), 'closure': copy.deepcopy(run['closure']), 'at': now()})
        for submission in run['submissions']:
            if key(submission['identity']) in run['coverage']['required']:
                submission.update(state='unknown', evidence=None, observedAt=now())
        run['coverage']['state'] = 'unknown' if run['coverage']['required'] else 'unsubmitted'
        run['closure'].update(state='held', evidenceIds=[], manualCommand=None, reason=reason)
        run['cleanup'] = {'state': 'held', 'blockers': ['manual_cleanup_required'], 'checkedAt': now()}

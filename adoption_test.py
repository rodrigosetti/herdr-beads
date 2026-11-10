"""Read-only migration of exact legacy conversations and acknowledged processes."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).parent / 'bin'))
from beads_contract import Refusal
from beads_herdr import HerdrHosting
from beads_omp import OmpExecution
from omp_adapter_test import Authority


class AdoptionHosting(HerdrHosting):
    def __init__(self):
        self.terminals = {}
        self.focused = []
        self.starts = []

    def call(self, argv):
        if argv[:2] == ['hc', 'ls']:
            return {'terminals': [{'address': address} for address in self.terminals]}
        if argv[:2] == ['hc', 'get']:
            return copy.deepcopy(self.terminals[argv[2]])
        if argv == ['herdr-app', 'ls']:
            return {'spaces': [{'id': 'space', 'tabs': [{'id': 'tab', 'terminals': [
                {'terminal': address} for address in self.terminals]}]}]}
        raise AssertionError(f'Unexpected hosting mutation: {argv}')

    def app(self, command_name, **args):
        if command_name != 'agent.focus':
            raise AssertionError(f'Unexpected terminal creation: {command_name}')
        self.focused.append(args['terminal'])
        return {}

    def start(self, run, argv, *, issue):
        self.starts.append(argv)

    def find_operation(self, run, path, *, address=None):
        return None


class LegacyAdoptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'xdg')})
        environment.start()
        self.addCleanup(environment.stop)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.beads = self.repo / '.beads'
        self.beads.mkdir()
        self.worktree = self.root / 'original-workspace'
        self.worktree.mkdir()
        self.transcript = self.root / 'exact-transcript.jsonl'
        self.record = {'session_id': 'exact-session', 'session_file': str(self.transcript),
                       'machine': 'machine', 'worktree': str(self.worktree)}
        self.transcript.write_text(json.dumps({'type': 'session_title', 'title': 'Not identity'}) + '\n'
                                   + json.dumps({'type': 'session', 'id': 'exact-session', 'cwd': str(self.worktree)})
                                   + '\n' + json.dumps({'type': 'message', 'message': {'role': 'user', 'content': 'Retain all history'}}) + '\n')
        self.authority = Authority({'id': 'task-1', 'title': 'Original title', 'status': 'in_progress',
                                    'metadata': {'omp_session': self.record, 'unrelated': {'keep': True}}})
        self.hosting = AdoptionHosting()
        self.adapter = OmpExecution(self.authority, self.hosting, self.root / 'controller-omp')
        self.adapter.binary = lambda name: name
        self.lease = 'free'
        self.adapter.lease = lambda record: self.lease
        self.run = {'scope': {'machineId': 'machine', 'issueId': 'task-1', 'beadsDir': str(self.beads),
                              'repository': {'root': str(self.repo), 'commonDir': str(self.repo / '.git')}},
                    'fence': {'runId': str(uuid.uuid4()), 'attempt': 1, 'generation': 1},
                    'execution': {'session': None, 'terminal': None, 'access': 'writable'}, 'workspace': None}
        self.private = {'bindingId': 'adopt-binding', 'credential': 'secret', 'sequence': 0}
        self.intent = {'operationId': str(uuid.uuid4()), 'steps': ['adopt'], 'index': 0}
        self.directory = self.root / 'xdg/go-bd' / hashlib.sha256(str(self.beads).encode()).hexdigest()
        self.directory.mkdir(parents=True)
        self.receipt_path = self.directory / 'issue-task-1.json'

    def write(self, path, value):
        path.write_text(json.dumps(value) + '\n')

    def launch(self):
        path = self.directory / f'launch-{uuid.uuid4()}.json'
        config = {'issue': 'task-1', 'root': str(self.repo), 'beads': str(self.beads),
                  'worktree': str(self.worktree), 'machine': 'machine', 'previous': None,
                  'bd': '/bin/bd', 'ack': str(path.with_name(path.stem + '-ack.json'))}
        self.write(path, config)
        self.write(Path(config['ack']), {'session': self.record, 'pid': 123})
        return path, config

    def active(self):
        path, config = self.launch()
        self.write(self.receipt_path, {'terminal': 't1', 'space': 'space', 'tab': 'tab'})
        self.hosting.terminals['t1'] = {'address': 't1', 'cwd': str(self.worktree), 'state': 'live',
            'labels': {'beads.project': str(self.beads), 'beads.issue': 'task-1'},
            'argv': ['env', f'BEADS_DIR={self.beads}', f'GO_BD_SESSION_CONFIG={path}', 'omp'],
            'foreground': {'name': 'omp', 'argv': ['omp'], 'pid': 123}, 'status': {'app': 'omp'}}
        self.lease = 'held'
        return path, config

    def adopt(self):
        return self.adapter.adopt(self.run, self.intent, self.private)

    def bind(self, result):
        self.run['workspace'] = {'path': result['workspacePath']}
        self.run['execution'].update(session=result['session'], terminal=result['terminal'])
        self.private['adapter'] = result['private']

    def refused(self, code=None):
        before_issue = copy.deepcopy(self.authority.row)
        before_transcript = self.transcript.read_bytes()
        before_files = {path: path.read_bytes() for path in self.directory.iterdir() if path.suffix == '.json'}
        with self.assertRaises(Refusal) as error:
            self.adopt()
        if code:
            self.assertEqual(error.exception.code, code)
        self.assertEqual(self.authority.row, before_issue)
        self.assertEqual(self.transcript.read_bytes(), before_transcript)
        self.assertEqual({path: path.read_bytes() for path in before_files}, before_files)
        self.assertEqual(self.hosting.starts, [])
        self.assertFalse(self.adapter.path(self.run).exists())

    def test_inactive_direct_session_preserves_exact_transcript_without_prompt_or_start(self):
        before = self.transcript.read_bytes()
        newer = self.root / 'latest.jsonl'
        self.write(newer, {'type': 'session', 'id': 'unrelated', 'cwd': str(self.worktree)})
        issue = copy.deepcopy(self.authority.row)
        result = self.adopt()
        self.assertEqual(result['activity'], 'inactive')
        self.assertIsNone(result['terminal'])
        self.assertEqual(result['session'], {'harness': 'omp', 'sessionId': 'exact-session', 'bindingId': 'adopt-binding'})
        self.assertEqual(result['private']['legacyAdoption']['record'], self.record)
        self.assertEqual(self.transcript.read_bytes(), before)
        self.assertEqual(self.authority.row, issue)
        self.assertEqual(self.hosting.starts, [])
        self.assertFalse(self.adapter.path(self.run).exists())
        self.assertEqual(list(self.root.glob('**/*.prompt.txt')), [])

    def test_acknowledged_live_session_adopts_and_focuses_without_relaunch(self):
        self.active()
        result = self.adopt()
        self.assertEqual(result['activity'], 'active')
        self.bind(result)
        self.assertEqual(self.adapter.inspect(self.run, self.private), 'active')
        self.assertEqual(self.adapter.perform('focus', self.run, self.intent, self.private), {'focused': True})
        self.assertEqual(self.hosting.focused, ['t1'])
        self.assertEqual(self.hosting.starts, [])

    def test_foreign_machine_is_not_adopted(self):
        self.record['machine'] = 'foreign'
        self.refused('remote_machine')

    def test_real_transcript_identity_conflict_is_not_adopted(self):
        self.record['session_id'] = 'wrong-session'
        self.refused('session_mismatch')

    def test_held_or_unknown_exact_lease_without_process_refuses(self):
        for lease in ('held', 'unknown'):
            with self.subTest(lease=lease):
                self.lease = lease
                self.refused('session_owned')

    def test_live_process_requires_held_exact_lease(self):
        self.active()
        self.lease = 'free'
        self.refused('session_owned')

    def test_generic_labels_never_authorize_live_session(self):
        self.active()
        self.hosting.terminals['t1']['argv'] = ['omp']
        self.refused('session_mismatch')

    def test_creation_argv_database_conflict_refuses(self):
        self.active()
        self.hosting.terminals['t1']['argv'][1] = f'BEADS_DIR={self.root}'
        self.refused('session_mismatch')

    def test_malformed_live_foreground_evidence_refuses(self):
        self.active()
        self.hosting.terminals['t1']['foreground'] = ['not', 'a', 'process']
        self.refused('terminal_mismatch')

    def test_live_foreground_pid_must_match_ack(self):
        self.active()
        self.hosting.terminals['t1']['foreground']['pid'] = 999
        self.refused('session_mismatch')

    def test_live_status_must_be_omp(self):
        self.active()
        self.hosting.terminals['t1']['status']['app'] = 'other'
        self.refused('session_mismatch')

    def test_receipt_label_conflict_refuses_without_relabeling(self):
        self.active()
        self.hosting.terminals['t1']['labels']['beads.issue'] = 'another-task'
        self.refused('terminal_mismatch')

    def test_receipt_layout_conflict_refuses(self):
        self.active()
        self.write(self.receipt_path, {'terminal': 't1', 'space': 'other-space', 'tab': 'tab'})
        self.refused('terminal_mismatch')

    def test_active_without_issue_receipt_refuses(self):
        self.active()
        self.receipt_path.unlink()
        self.refused('terminal_mismatch')

    def test_another_labeled_live_terminal_conflicts_with_receipt(self):
        self.active()
        self.hosting.terminals['t2'] = copy.deepcopy(self.hosting.terminals['t1'])
        self.hosting.terminals['t2']['address'] = 't2'
        self.refused('terminal_mismatch')

    def test_matching_config_canonical_context_conflicts_refuse(self):
        path, config = self.launch()
        for field, value in (('root', str(self.root)), ('beads', str(self.root)),
                             ('worktree', str(self.root)), ('machine', 'foreign')):
            with self.subTest(field=field):
                changed = dict(config, **{field: value})
                self.write(path, changed)
                self.refused()
        self.write(path, config)

    def test_malformed_or_orphan_matching_configuration_refuses(self):
        path, config = self.launch()
        for content in ('not JSON', 'null'):
            path.write_text(content)
            self.refused('invalid_record')
        self.write(path, config)
        Path(config['ack']).unlink()
        self.refused('session_mismatch')

    def test_conflicting_ack_session_is_not_treated_as_historical_evidence(self):
        path, config = self.launch()
        wrong = dict(self.record, session_id='other-session')
        self.write(Path(config['ack']), {'session': wrong, 'pid': 123})
        self.refused('session_mismatch')

    def test_orphan_exact_acknowledgement_refuses(self):
        path, config = self.launch()
        path.unlink()
        self.refused('invalid_record')

    def test_malformed_receipt_and_missing_metadata_refuse(self):
        self.write(self.receipt_path, {'terminal': 't1'})
        self.refused('invalid_record')
        self.receipt_path.unlink()
        del self.authority.row['metadata']['omp_session']
        self.refused('session_missing')

    def test_receipt_terminal_with_foreign_creation_context_refuses_even_when_ended(self):
        path, config = self.active()
        config['issue'] = 'other-task'
        self.write(path, config)
        self.hosting.terminals['t1']['foreground'] = {'name': 'zsh', 'pid': 456}
        self.lease = 'free'
        self.refused('session_mismatch')

    def test_inspect_reconciles_live_labels_against_retained_evidence(self):
        self.active()
        self.bind(self.adopt())
        self.hosting.terminals['t1']['labels']['beads.project'] = str(self.root)
        with self.assertRaises(Refusal) as error:
            self.adapter.inspect(self.run, self.private)
        self.assertEqual(error.exception.code, 'terminal_mismatch')

    def test_multiple_ended_launches_of_same_exact_session_are_preserved(self):
        self.launch()
        path, config = self.launch()
        config['previous'] = copy.deepcopy(self.record)
        self.write(path, config)
        result = self.adopt()
        self.assertEqual(result['activity'], 'inactive')
        self.assertEqual(len(result['private']['legacyAdoption']['launches']), 2)

    def test_other_issue_config_is_not_associated(self):
        path = self.directory / f'launch-{uuid.uuid4()}.json'
        self.write(path, {'issue': 'other-task', 'machine': 'foreign'})
        self.assertEqual(self.adopt()['activity'], 'inactive')

    def test_retained_shell_is_inactive_when_exact_lease_is_free(self):
        self.active()
        self.hosting.terminals['t1']['foreground'] = {'name': 'zsh', 'argv': ['zsh'], 'pid': 456}
        self.lease = 'free'
        self.assertEqual(self.adopt()['activity'], 'inactive')

    def test_retained_private_evidence_survives_legacy_launcher_files_removal(self):
        self.active()
        result = self.adopt()
        self.bind(result)
        for path in self.directory.glob('*.json'):
            path.unlink()
        self.assertEqual(self.adapter.inspect(self.run, self.private), 'active')
        self.hosting.terminals['t1']['foreground']['pid'] = 999
        with self.assertRaises(Refusal):
            self.adapter.inspect(self.run, self.private)

    def test_resume_uses_exact_history_new_binding_and_no_prompt(self):
        result = self.adopt()
        self.bind(result)
        self.run['fence']['generation'] += 1
        self.private['bindingId'] = 'new-generation-binding'
        self.intent['steps'] = ['resume']
        self.assertIsNone(self.adapter.perform('resume', self.run, self.intent, self.private))
        self.assertEqual(self.hosting.starts[0][-2:], ['--resume', str(self.transcript)])
        config = json.loads(self.adapter.path(self.run).read_text())
        self.assertEqual(config['bindingId'], 'new-generation-binding')
        self.assertNotIn('prompt_file', config)

    def test_stale_adoption_identity_cannot_authorize_new_generation_process(self):
        self.active()
        self.bind(self.adopt())
        self.run['fence']['generation'] += 1
        self.private['bindingId'] = 'new-generation-binding'
        self.assertEqual(self.adapter.inspect(self.run, self.private), 'unknown')

    def test_legacy_launcher_lock_contention_refuses_and_retains_lock_inode(self):
        path = self.directory / 'issue-task-1.lock'
        with path.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            inode = path.stat().st_ino
            self.refused('evidence_unavailable')
            self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(self.adopt()['activity'], 'inactive')
        self.assertEqual(path.stat().st_ino, inode)


if __name__ == '__main__':
    unittest.main()

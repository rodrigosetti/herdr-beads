"""Exact conversation/process boundaries; no model or Herdr dependency."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).parent / 'bin'))
from beads_contract import Refusal
from beads_omp import OmpExecution, durable_json, live_omp


class Authority:
    def __init__(self, issue):
        self.row = issue

    def issue(self, scope):
        return copy.deepcopy(self.row)


class Hosting:
    def __init__(self):
        self.starts = []
        self.current = None
        self.uncertain = False

    def preflight(self):
        pass

    def start(self, run, argv):
        self.starts.append(argv)
        if self.uncertain:
            raise Refusal('evidence_unavailable', 'reply lost', True)

    def find_operation(self, run, path, *, address=None):
        return self.current

    def associate(self, run, address):
        pass

    def info(self, address):
        return self.current

    def lookup(self, address):
        return self.current

    def validate(self, run, info):
        if info['cwd'] != run['workspace']['path']:
            raise Refusal('terminal_mismatch', 'wrong workspace')

    def terminal(self, run, info):
        return {'machineId': 'machine', 'address': info['address'], 'spaceId': None, 'tabId': None}


class OmpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.worktree = self.root / 'workspace'
        self.worktree.mkdir()
        self.transcript = self.root / 'exact.jsonl'
        self.record = {'session_id': 'exact-session', 'session_file': str(self.transcript),
                       'machine': 'machine', 'worktree': str(self.worktree)}
        self.transcript.write_text(json.dumps({'type': 'session', 'id': 'exact-session', 'cwd': str(self.worktree)}) + '\n')
        self.authority = Authority({'id': 'task-1', 'status': 'in_progress', 'metadata': {'other': {'keep': True}}})
        self.hosting = Hosting()
        self.adapter = OmpExecution(self.authority, self.hosting, self.root / 'state')
        self.adapter.binary = lambda name: name
        self.adapter.lease = lambda record: 'free'
        self.run = {'scope': {'machineId': 'machine', 'issueId': 'task-1', 'beadsDir': str(self.root / '.beads'),
                             'repository': {'root': str(self.root / 'repo'), 'commonDir': str(self.root / 'repo/.git')}},
                    'fence': {'runId': str(uuid.uuid4()), 'attempt': 1, 'generation': 1},
                    'execution': {'session': None, 'terminal': None, 'access': 'writable'},
                    'workspace': {'path': str(self.worktree)}}
        self.private = {'bindingId': 'binding', 'credential': 'secret', 'sequence': 0}
        self.intent = {'operationId': str(uuid.uuid4()), 'steps': ['launch'], 'index': 0}

    def previous(self):
        self.authority.row['metadata']['omp_session'] = self.record
        self.run['execution']['session'] = {'harness': 'omp', 'sessionId': 'exact-session', 'bindingId': 'old-binding'}
        self.intent['steps'] = ['resume']

    def ack(self, config):
        self.authority.row['metadata']['omp_session'] = self.record
        durable_json(Path(config['ack']), {'session': self.record, 'pid': 123})
        self.hosting.current = {'address': 't1', 'cwd': str(self.worktree), 'state': 'live',
                                'foreground': {'name': 'omp', 'argv': ['omp'], 'pid': 123}, 'status': {'app': 'omp'}}
        self.adapter.lease = lambda record: 'held'

    def test_uncertain_creation_recovers_exact_session_without_second_terminal(self):
        self.hosting.uncertain = True
        with self.assertRaises(Refusal):
            self.adapter.perform('launch', self.run, self.intent, self.private)
        path = self.adapter.path(self.run)
        config = json.loads(path.read_text())
        self.assertEqual(config['operationId'], self.intent['operationId'])
        self.assertIsNone(self.adapter.recover('launch', self.run, self.intent, self.private))
        self.ack(config)
        result = self.adapter.recover('launch', self.run, self.intent, self.private)
        self.adapter.validate_observation(self.run, result['observation'], self.private)
        self.assertEqual(result['observation']['event']['session']['sessionId'], 'exact-session')
        self.assertEqual(len(self.hosting.starts), 1)
        self.assertEqual(self.authority.row['metadata']['other'], {'keep': True})

    def test_resume_uses_exact_transcript_without_new_prompt_or_restore(self):
        self.previous()
        self.assertIsNone(self.adapter.perform('resume', self.run, self.intent, self.private))
        argv = self.hosting.starts[0]
        self.assertEqual(argv[-2:], ['--resume', str(self.transcript)])
        config = json.loads(self.adapter.path(self.run).read_text())
        self.assertNotIn('prompt_file', config)
        self.assertNotIn('--restore', argv)

    def test_foreign_missing_mismatched_and_leased_history_refuse_before_terminal(self):
        self.previous()
        original = copy.deepcopy(self.authority.row)
        for field, value, code in [('machine', 'foreign', 'remote_machine'),
                                   ('session_id', 'other', 'session_mismatch'),
                                   ('session_file', str(self.root / 'missing'), 'session_missing')]:
            with self.subTest(field=field):
                self.record[field], old = value, self.record[field]
                with self.assertRaises(Refusal) as error:
                    self.adapter.perform('resume', self.run, self.intent, self.private)
                self.assertEqual(error.exception.code, code)
                self.record[field] = old
        for lease in ('held', 'unknown'):
            self.adapter.lease = lambda record: lease
            with self.assertRaises(Refusal) as error:
                self.adapter.perform('resume', self.run, self.intent, self.private)
            self.assertEqual(error.exception.code, 'session_owned')
        self.assertEqual(self.hosting.starts, [])
        self.assertEqual(self.authority.row, original)

    def test_retained_shell_and_stale_agent_report_do_not_count_as_active_omp(self):
        self.previous()
        self.hosting.current = {'address': 't1', 'cwd': str(self.worktree), 'state': 'live',
                                'foreground': {'name': 'zsh', 'argv': ['zsh'], 'pid': 456}, 'status': {'app': 'omp'}}
        self.run['execution']['terminal'] = {'address': 't1'}
        self.assertFalse(live_omp(self.hosting.current))
        self.assertEqual(self.adapter.inspect(self.run, self.private), 'inactive')
        self.adapter.lease = lambda record: 'held'
        with self.assertRaises(Refusal) as error:
            self.adapter.inspect(self.run, self.private)
        self.assertEqual(error.exception.code, 'session_owned')

    def test_ack_requires_same_foreground_pid_not_just_a_live_omp_report(self):
        config = self.adapter.config(self.run, self.intent, self.private)
        self.ack(config)
        self.hosting.current['foreground']['pid'] = 999
        self.assertIsNone(self.adapter.recover('launch', self.run, self.intent, self.private))
        self.assertEqual(self.authority.row['metadata']['omp_session']['session_id'], 'exact-session')

    def test_controller_binding_validates_before_metadata_mirror_and_rejects_wrong_process(self):
        config = self.adapter.config(self.run, self.intent, self.private)
        self.ack(config)
        observation = self.adapter.recover('launch', self.run, self.intent, self.private)['observation']
        binding = {'schemaVersion': 1, 'fence': self.run['fence'],
                   'binding': observation['event']['session'], 'machineId': 'machine',
                   'workspacePath': str(self.worktree), 'omp': self.record,
                   'pid': 123, 'observation': observation}
        durable_json(Path(str(self.adapter.path(self.run)) + '.binding.json'), binding)
        del self.authority.row['metadata']['omp_session']
        self.adapter.validate_observation(self.run, observation, self.private)
        self.assertNotIn('omp_session', self.authority.row['metadata'])
        self.hosting.current['foreground']['pid'] = 456
        with self.assertRaises(Refusal) as error:
            self.adapter.validate_observation(self.run, observation, self.private)
        self.assertEqual(error.exception.code, 'session_mismatch')
        self.assertNotIn('omp_session', self.authority.row['metadata'])

    def test_legacy_metadata_cannot_be_overwritten_by_new_run(self):
        self.authority.row['metadata']['omp_session'] = self.record
        before = self.transcript.read_bytes()
        with self.assertRaises(Refusal) as error:
            self.adapter.preflight(self.run)
        self.assertEqual(error.exception.code, 'session_owned')
        self.assertEqual(self.transcript.read_bytes(), before)
        self.assertFalse(self.adapter.path(self.run).exists())


if __name__ == '__main__':
    unittest.main()

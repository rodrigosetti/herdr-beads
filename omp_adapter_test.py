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
from beads_herdr import HerdrHosting
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

    def start(self, run, argv, *, issue):
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


class MemoryHerdr(HerdrHosting):
    """Stateful local app boundary; mutations change the next listing."""
    def __init__(self, spaces=None, terminals=None):
        self.rows = copy.deepcopy(spaces or [])
        self.terminals = copy.deepcopy(terminals or {})
        self.events = []
        self.rename_error = None

    def call(self, argv):
        if argv == ['herdr-app', 'ls']:
            return {'spaces': copy.deepcopy(self.rows)}
        if argv[:2] == ['hc', 'ls']:
            return {'terminals': [copy.deepcopy(info) for info in self.terminals.values()]}
        if argv[:2] == ['hc', 'get']:
            return copy.deepcopy(self.terminals[argv[2]])
        if argv[:2] == ['hc', 'label']:
            labels = self.terminals[argv[2]].setdefault('labels', {})
            labels.update(arg.split('=', 1) for arg in argv[3:] if arg != '--json')
            return {}
        raise AssertionError(f'Unexpected hosting command: {argv}')

    def app(self, command_name, **args):
        self.events.append((command_name, copy.deepcopy(args)))
        if command_name == 'space.rename':
            if self.rename_error:
                raise self.rename_error
            space = next(row for row in self.rows if row['id'] == args['id'])
            space['name'] = args['name']
            return {}
        if command_name == 'agent.focus':
            assert args['terminal'] in self.terminals
            return {}
        if command_name == 'space.new':
            space = {'id': 'new-space', 'folder': args['folder'], 'name': args['name'], 'tabs': []}
            self.rows.append(space)
        elif command_name == 'tab.new':
            space = next(row for row in self.rows if row['id'] == args['space'])
        else:
            raise AssertionError(f'Unexpected app mutation: {command_name}')
        address = f't{len(self.terminals) + 1}'
        tab = {'id': 'new-tab', 'terminals': [{'terminal': address}]}
        space.setdefault('tabs', []).append(tab)
        self.terminals[address] = {'address': address, 'cwd': args['folder'],
                                   'argv': args['argv'], 'state': 'live', 'labels': {}}
        return {'terminal': address, 'space': space['id'], 'tab': tab['id']}


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
        self.authority = Authority({'id': 'task-1', 'title': 'Original title', 'status': 'in_progress',
                                    'metadata': {'other': {'keep': True}}})
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

    def test_launch_uses_current_title_without_changing_workspace_or_fence(self):
        self.authority.row['title'] = 'Current: title révisé'
        self.run['issueObservation'] = {'title': 'Stale title'}
        original = copy.deepcopy(self.run)
        self.adapter.hosting = hosting = MemoryHerdr()
        self.assertIsNone(self.adapter.perform('launch', self.run, self.intent, self.private))
        self.assertEqual(hosting.rows[0]['name'], 'Current: title révisé')
        self.assertEqual(self.run, original)
        self.assertEqual(hosting.terminals['t1']['cwd'], str(self.worktree))

    def test_reuse_renames_only_exact_local_folder_before_creating_tab(self):
        other = self.root / 'other'
        other.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(self.worktree, target_is_directory=True)
        hosting = MemoryHerdr([
            {'id': 'remote', 'machine': 'foreign', 'folder': str(self.worktree), 'name': 'Old remote'},
            {'id': 'other', 'folder': str(other), 'name': 'task-1: Original title'},
            {'id': 'retained', 'folder': str(alias), 'name': 'task-1', 'tabs': [
                {'id': 'original-tab', 'terminals': [{'terminal': 'original-terminal'}]}]},
        ])
        before = copy.deepcopy(self.run)
        result = hosting.start(self.run, ['omp'], issue=self.authority.issue(self.run['scope']))
        self.assertEqual([event[0] for event in hosting.events], ['space.rename', 'tab.new'])
        self.assertEqual(result['space'], 'retained')
        self.assertEqual(hosting.rows[2]['name'], 'Original title')
        self.assertEqual(hosting.rows[2]['folder'], str(alias))
        self.assertEqual(hosting.rows[2]['tabs'][0]['id'], 'original-tab')
        self.assertEqual(hosting.rows[2]['tabs'][0]['terminals'][0]['terminal'], 'original-terminal')
        self.assertEqual(hosting.rows[0]['name'], 'Old remote')
        self.assertEqual(hosting.rows[1]['folder'], str(other))
        self.assertEqual(self.run, before)

    def test_focus_reconciles_latest_title_without_replacing_exact_session_or_terminal(self):
        self.previous()
        config = self.adapter.config(self.run, self.intent, self.private)
        self.ack(config)
        info = copy.deepcopy(self.hosting.current)
        info['labels'] = {'beads.project': self.run['scope']['beadsDir'], 'beads.issue': 'task-1'}
        hosting = MemoryHerdr([{'id': 'retained', 'folder': str(self.worktree), 'name': 'Old title',
                               'tabs': [{'id': 'original-tab', 'terminals': [{'terminal': 't1'}]}]}],
                              {'t1': info})
        self.adapter.hosting = hosting
        self.run['execution']['terminal'] = hosting.terminal(self.run, info)
        self.authority.row['title'] = 'Edited title'
        original = copy.deepcopy(self.run)
        record = copy.deepcopy(self.authority.row['metadata'])
        transcript = self.transcript.read_bytes()
        self.assertEqual(self.adapter.perform('focus', self.run, self.intent, self.private), {'focused': True})
        self.assertEqual(hosting.events, [('space.rename', {'id': 'retained', 'name': 'Edited title'}),
                                          ('agent.focus', {'terminal': 't1'})])
        self.assertEqual(self.run, original)
        self.assertEqual(self.authority.row['metadata'], record)
        self.assertEqual(self.transcript.read_bytes(), transcript)
        self.assertEqual(hosting.rows[0]['tabs'][0]['id'], 'original-tab')
        self.adapter.perform('focus', self.run, self.intent, self.private)
        self.assertEqual([event[0] for event in hosting.events].count('space.rename'), 1)


if __name__ == '__main__':
    unittest.main()

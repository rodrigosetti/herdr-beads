"""Display reconciliation must never substitute names for stable associations."""
import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).parent / 'bin'))
from beads_presentation import reconcile_project_spaces, reconcile_space_names


class SpaceNamesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.worktree = str(self.root / 'original-worktree')
        Path(self.worktree).mkdir()
        self.issue = {'id': 'project-123.4', 'title': 'Fix "quotes": /paths/ & café',
                      'metadata': {'omp_session': {'machine': 'local', 'worktree': self.worktree,
                                   'session_id': 'conversation', 'session_file': '/saved/session.jsonl'}}}
        self.spaces = [
            {'id': 'original-space', 'folder': self.worktree, 'name': 'project-123.4',
             'tabs': [{'id': 'original-tab', 'terminals': [{'terminal': 't1'}]}]},
            {'id': 'remote', 'folder': self.worktree, 'name': 'remote name', 'machine': 'remote'},
            {'id': 'other', 'folder': str(self.root), 'name': 'project-123.4'},
        ]
        self.terminal = {'address': 't1', 'cwd': self.worktree,
                         'labels': {'beads.project': '/canonical/.beads', 'beads.issue': self.issue['id']}}
        self.commands = []
        self.project = SimpleNamespace(beads='/canonical/.beads', bd=self.bd)
        self.hosting = SimpleNamespace(spaces=lambda: copy.deepcopy(self.spaces), call=self.call, app=self.app)

    def bd(self, *args):
        self.assertEqual(args, ('show', self.issue['id']))
        return [copy.deepcopy(self.issue)]

    def call(self, argv):
        if argv[1] == 'ls':
            return {'terminals': [{'address': 't1', 'cwd': self.worktree}]}
        return copy.deepcopy(self.terminal)

    def app(self, command, **args):
        self.commands.append((command, args))
        self.assertEqual(command, 'space.rename')
        space = next(space for space in self.spaces if space['id'] == args['id'])
        space['name'] = args['name']

    def reconcile(self):
        return reconcile_project_spaces(self.project, self.hosting, 'local')

    def test_current_title_replaces_stale_label_without_replacing_identity(self):
        before = copy.deepcopy(self.spaces)
        association = copy.deepcopy(self.issue['metadata'])
        self.assertEqual(self.reconcile(), {'renamed': ['original-space'], 'errors': []})
        self.assertEqual(self.spaces[0]['name'], 'Fix "quotes": /paths/ & café')
        self.issue['title'] = 'Changed title: keep the same conversation'
        self.assertEqual(self.reconcile()['renamed'], ['original-space'])
        self.assertEqual(self.spaces[0]['name'], 'Changed title: keep the same conversation')
        self.assertEqual(self.reconcile()['renamed'], [])
        self.assertEqual(len(self.commands), 2)
        after = copy.deepcopy(self.spaces)
        after[0]['name'] = before[0]['name']
        self.assertEqual(after, before)
        self.assertEqual(self.issue['metadata'], association)
        self.assertTrue(Path(self.worktree).is_dir())

    def test_stale_foreign_or_moved_associations_do_not_relabel(self):
        original = copy.deepcopy(self.issue)
        for change in ({'machine': 'foreign'}, {'worktree': str(self.root / 'other')},
                       {'worktree': 'relative/path'}):
            with self.subTest(change=change):
                self.issue = copy.deepcopy(original)
                self.issue['metadata']['omp_session'].update(change)
                self.assertEqual(self.reconcile()['renamed'], [])
        self.issue = original
        self.terminal['labels']['beads.project'] = '/different/.beads'
        self.assertEqual(self.reconcile()['renamed'], [])
        self.assertEqual(self.commands, [])

    def test_prefix_resolution_is_not_authority_to_rename(self):
        self.project.bd = lambda *args: [{**self.issue, 'id': 'another-issue'}]
        result = self.reconcile()
        self.assertEqual(result['renamed'], [])
        self.assertIn('full exact issue', result['errors'][0]['error'])
        self.assertEqual(self.commands, [])

    def test_every_local_space_for_exact_workspace_is_updated(self):
        self.spaces.append({'id': 'same-workspace', 'folder': self.worktree, 'name': 'custom'})
        snapshot = copy.deepcopy(self.spaces)
        renamed = reconcile_space_names(self.issue, self.worktree, snapshot, self.app)
        self.assertEqual(renamed, ['original-space', 'same-workspace'])
        self.assertEqual(snapshot[0]['name'], 'project-123.4')
        self.assertEqual(self.spaces[1]['name'], 'remote name')
        self.assertEqual(self.spaces[2]['name'], 'project-123.4')

    def test_duplicate_titles_do_not_coalesce_distinct_workspaces(self):
        second = {'id': 'project-999', 'title': self.issue['title']}
        reconcile_space_names(self.issue, self.worktree, copy.deepcopy(self.spaces), self.app)
        reconcile_space_names(second, str(self.root), copy.deepcopy(self.spaces), self.app)
        self.assertEqual(self.spaces[0]['name'], self.spaces[2]['name'])
        self.assertEqual(self.spaces[0]['id'], 'original-space')
        self.assertEqual(self.spaces[2]['id'], 'other')
        self.assertNotEqual(self.spaces[0]['folder'], self.spaces[2]['folder'])


if __name__ == '__main__':
    unittest.main()

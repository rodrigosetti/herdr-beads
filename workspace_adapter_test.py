"""Concrete workspace protocol with real local Git and a deterministic omp CLI.

Only omp's public config/registry/add boundary is replaced. No remote service,
installed omp configuration, or execution adapter is required.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'bin'))
from beads_contract import Refusal, validate
from beads_identity import command, repository_identity
from beads_workspace import OmpWorkspace


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True, stderr=subprocess.PIPE).strip()


class WorkspaceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'source').write_text('base\n')
        git(self.repo, 'add', 'source')
        git(self.repo, 'commit', '-m', 'initial')
        self.initial = git(self.repo, 'rev-parse', 'HEAD')
        self.source = repository_identity(str(self.repo))
        self.run = {'scope': {'repository': self.source, 'issueId': 'test-1',
                             'beadsDir': str(self.repo / '.beads'), 'machineId': 'test-machine'},
                    'fence': {'runId': 'test-run', 'attempt': 1, 'generation': 1},
                    'target': {'mode': 'local-target', 'repository': copy.deepcopy(self.source), 'ref': 'refs/heads/main'},
                    'workspace': None}
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(('GIT_', 'PI_', 'OMP_', 'XDG_'))}
        self.env['HOME'] = str(self.home)
        self.provider = OmpWorkspace(env=self.env)
        self.base_setting = None
        self.registry = []
        self.calls = []
        self.add_calls = 0
        self.crash_after_add = False
        self.clone_setting = False
        self.clone_modes_seen = []
        patcher = patch.object(self.provider, '_command', side_effect=self.cli)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, argv, run, *, timeout=20, cwd=None):
        argv = [str(arg) for arg in argv]
        self.calls.append(argv)
        if argv[:5] == ['omp', 'config', 'get', 'worktree.base', '--json']:
            value = {'key': 'worktree.base', 'type': 'string'}
            if self.base_setting is not None:
                value['value'] = self.base_setting
            return json.dumps(value)
        if argv == ['omp', 'worktree', 'list', '--json']:
            return json.dumps(self.registry)
        if argv[:3] == ['omp', 'worktree', 'add']:
            self.add_calls += 1
            self.clone_modes_seen.append(self.clone_setting)
            root = argv[argv.index('-C') + 1]
            branch = argv[argv.index('-b') + 1]
            path, target = argv[-2:]
            # Installed omp's clone mode changes copying, not Git identity:
            # both copy-on-write and fallback register a linked worktree.
            git(root, 'worktree', 'add', '-b', branch, path, target)
            self.registry.append({'path': path, 'kind': 'pr-checkout', 'parentRepo': root, 'branch': branch})
            if self.crash_after_add:
                raise Refusal('evidence_unavailable', 'interrupted after provisioning', True)
            return ''
        if argv[0] == 'omp':
            self.fail(f'Unexpected omp command: {argv}')
        return command(argv, cwd or self.repo, env=self.env, timeout=timeout)

    def planned(self):
        workspace = self.provider.plan(self.run)
        validate('run', workspace, 'Workspace')
        return workspace, {'plannedWorkspace': copy.deepcopy(workspace)}

    def provision(self):
        workspace, private = self.planned()
        receipt = self.provider.perform('workspace', self.run, {}, private)
        self.assertEqual(receipt, {'workspace': workspace})
        self.run['workspace'] = workspace
        return workspace, private

    def assert_refusal(self, code, action):
        with self.assertRaises(Refusal) as raised:
            action()
        self.assertEqual(raised.exception.code, code)

    def test_plan_predicts_exact_linked_identity_without_effects_or_remote(self):
        before = git(self.repo, 'show-ref')
        workspace, _ = self.planned()
        self.assertEqual(workspace, {'provider': 'omp-worktree', 'backing': 'linked',
            'path': str(self.home / '.omp/wt/test-1'), 'branchRef': 'refs/heads/test-1',
            'sourceRepository': self.source, 'commonDir': self.source['commonDir']})
        self.assertFalse(Path(workspace['path']).exists())
        self.assertFalse((self.home / '.omp').exists())
        self.assertEqual(git(self.repo, 'show-ref'), before)
        self.assertEqual(git(self.repo, 'remote'), '')
        self.assertEqual(self.add_calls, 0)

    def test_perform_uses_declared_target_not_current_head(self):
        git(self.repo, 'checkout', '-b', 'unrelated')
        (self.repo / 'source').write_text('unrelated\n')
        git(self.repo, 'commit', '-am', 'unrelated')
        workspace, _ = self.provision()
        self.assertEqual(git(workspace['path'], 'rev-parse', 'HEAD'), self.initial)
        self.assertEqual(self.add_calls, 1)
        self.assertEqual(self.provider.validate(self.run), workspace)

    def test_copy_on_write_configuration_does_not_change_git_backing(self):
        self.clone_setting = True
        workspace, _ = self.provision()
        self.assertEqual(self.clone_modes_seen, [True])
        self.assertEqual(workspace['backing'], 'linked')
        self.assertEqual(workspace['commonDir'], self.source['commonDir'])

    def test_configured_base_and_environment_override_are_honored(self):
        self.base_setting = '~/configured'
        self.assertEqual(self.provider.plan(self.run)['path'], str(self.home / 'configured/test-1'))
        self.provider.env['OMP_WORKTREE_DIR'] = str(self.home / 'environment')
        self.assertEqual(self.provider.plan(self.run)['path'], str(self.home / 'environment/test-1'))
        self.provider.env['OMP_WORKTREE_DIR'] = 'relative-is-ignored'
        self.assertEqual(self.provider.plan(self.run)['path'], str(self.home / 'configured/test-1'))
        self.base_setting = 'also-relative'
        self.assertEqual(self.provider.plan(self.run)['path'], str(self.home / '.omp/wt/test-1'))

    def test_default_base_follows_profile_and_migrated_xdg_location(self):
        self.provider.env['OMP_PROFILE'] = 'work'
        self.provider.env['XDG_DATA_HOME'] = str(self.home / 'xdg')
        expected = self.home / '.omp/profiles/work/wt/test-1'
        self.assertEqual(self.provider.plan(self.run)['path'], str(expected))
        migrated = self.home / 'xdg/omp/profiles/work'
        migrated.mkdir(parents=True)
        self.assertEqual(self.provider.plan(self.run)['path'], str(migrated / 'wt/test-1'))

    def test_new_attempt_has_distinct_issue_named_path_and_branch(self):
        first, _ = self.provision()
        self.run['workspace'] = None
        self.run['fence']['attempt'] = 2
        second, _ = self.provision()
        self.assertNotEqual(first['path'], second['path'])
        self.assertEqual(second['branchRef'], 'refs/heads/test-1-attempt-2')
        self.assertTrue(Path(first['path']).is_dir())
        self.assertEqual(git(first['path'], 'symbolic-ref', 'HEAD'), first['branchRef'])

    def test_recovery_observes_provisioning_after_interruption_without_retry(self):
        workspace, private = self.planned()
        self.crash_after_add = True
        self.assert_refusal('evidence_unavailable', lambda: self.provider.perform('workspace', self.run, {}, private))
        self.calls.clear()
        self.assertEqual(self.provider.recover('workspace', self.run, {}, private), {'workspace': workspace})
        self.assertEqual(self.add_calls, 1)
        self.assertFalse(any(argv[:3] == ['omp', 'worktree', 'add'] for argv in self.calls))

    def test_unknown_or_partial_provisioning_is_never_recreated(self):
        workspace, private = self.planned()
        self.assertIsNone(self.provider.recover('workspace', self.run, {}, private))
        Path(workspace['path']).mkdir(parents=True)
        self.assertIsNone(self.provider.recover('workspace', self.run, {}, private))
        self.assertEqual(self.add_calls, 0)

    def test_existing_linked_workspace_is_adopted_only_with_exact_branch(self):
        workspace, _ = self.provision()
        self.assertEqual(self.provider.plan(self.run), workspace)
        git(workspace['path'], 'checkout', '-b', 'wrong')
        self.assert_refusal('workspace_mismatch', lambda: self.provider.plan(self.run))
        self.assert_refusal('workspace_mismatch', lambda: self.provider.validate(self.run))

    def test_existing_registry_path_is_respected_without_default_directory(self):
        path = self.home / 'legacy-location'
        git(self.repo, 'worktree', 'add', '-b', 'test-1', str(path))
        self.registry = [{'path': str(path), 'parentRepo': str(self.repo), 'branch': 'test-1', 'kind': 'pr-checkout'}]
        workspace, private = self.planned()
        self.assertEqual(workspace['path'], str(path))
        self.assertEqual(self.provider.perform('workspace', self.run, {}, private), {'workspace': workspace})
        self.assertEqual(self.add_calls, 0)

    def test_independent_clone_requires_canonical_source_registry_not_origin(self):
        path = self.home / '.omp/wt/test-1'
        path.parent.mkdir(parents=True)
        git(self.home, 'clone', '--no-hardlinks', str(self.repo), str(path))
        git(path, 'checkout', '-b', 'test-1')
        self.assertEqual(git(path, 'remote', 'get-url', 'origin'), str(self.repo))
        self.assert_refusal('workspace_mismatch', lambda: self.provider.plan(self.run))
        self.registry = [{'path': str(path), 'parentRepo': str(self.repo), 'branch': 'test-1', 'kind': 'pr-checkout'}]
        workspace, private = self.planned()
        self.assertEqual(workspace['backing'], 'clone')
        self.assertEqual(workspace['commonDir'], str(path / '.git'))
        git(path, 'remote', 'set-url', 'origin', 'https://unrelated.invalid/repository')
        self.assertEqual(self.provider.recover('workspace', self.run, {}, private), {'workspace': workspace})
        self.run['workspace'] = workspace
        self.registry[0]['parentRepo'] = str(self.home / 'another-repository')
        self.assert_refusal('workspace_mismatch', lambda: self.provider.validate(self.run))


    def test_explicit_linked_adoption_preserves_branch_without_target_or_effects(self):
        path = self.home / 'recorded-legacy-location'
        branch = 'feature/original-session'
        git(self.repo, 'worktree', 'add', '-b', branch, str(path))
        self.registry = [{'path': str(path), 'parentRepo': str(self.repo), 'branch': branch}]
        self.run['target'] = None
        self.base_setting = str(self.home / 'must-not-infer-destination')
        before = git(self.repo, 'show-ref')
        registry = copy.deepcopy(self.registry)
        workspace = self.provider.adopt(self.run, str(path))
        self.assertEqual(workspace, {'provider': 'omp-worktree', 'backing': 'linked',
            'path': str(path), 'branchRef': f'refs/heads/{branch}',
            'sourceRepository': self.source, 'commonDir': self.source['commonDir']})
        self.run['workspace'] = workspace
        self.assertEqual(self.provider.validate(self.run), workspace)
        self.assertIsNone(self.run['target'])
        self.assertEqual(git(self.repo, 'show-ref'), before)
        self.assertEqual(self.registry, registry)
        self.assertEqual(self.add_calls, 0)
        self.registry = []
        self.assert_refusal('workspace_mismatch', lambda: self.provider.validate(self.run))
        self.registry = registry
        git(path, 'checkout', '-b', 'changed-after-adoption')
        self.assert_refusal('workspace_mismatch', lambda: self.provider.validate(self.run))

    def test_explicit_clone_adoption_requires_registry_and_ignores_origin(self):
        path = self.home / 'retained-independent-clone'
        git(self.home, 'clone', '--no-hardlinks', str(self.repo), str(path))
        git(path, 'checkout', '-b', 'historical-branch')
        self.run['target'] = None
        self.assert_refusal('workspace_mismatch', lambda: self.provider.adopt(self.run, str(path)))
        self.registry = [{'path': str(path), 'parentRepo': str(self.repo), 'branch': 'historical-branch'}]
        git(path, 'remote', 'set-url', 'origin', 'https://unrelated.invalid/not-authority')
        workspace = self.provider.adopt(self.run, str(path))
        self.assertEqual(workspace['backing'], 'clone')
        self.assertEqual(workspace['commonDir'], str(path / '.git'))
        self.assertEqual(workspace['branchRef'], 'refs/heads/historical-branch')
        self.run['workspace'] = workspace
        self.assertEqual(self.provider.validate(self.run), workspace)
        self.assertEqual(self.add_calls, 0)
        self.assertFalse(any('fetch' in argv or 'push' in argv or 'merge' in argv for argv in self.calls))
        exact = self.registry[0]
        for rows in ([exact, exact], [dict(exact, orphanReason='missing parent')],
                     [dict(exact, parentRepo=str(self.home))]):
            self.registry = rows
            self.assert_refusal('workspace_mismatch', lambda: self.provider.adopt(self.run, str(path)))

    def test_adoption_refuses_unregistered_or_conflicting_linked_workspace(self):
        path = self.home / 'linked-legacy'
        git(self.repo, 'worktree', 'add', '-b', 'legacy-branch', str(path))
        self.run['target'] = None
        exact = {'path': str(path), 'parentRepo': str(self.repo), 'branch': 'legacy-branch'}
        variants = ([], [dict(exact, orphanReason='missing parent')], [dict(exact, orphanReason='')],
                    [dict(exact, kind='orphaned')], [dict(exact, branch='wrong')],
                    [dict(exact, parentRepo=str(self.home))], [dict(exact, parentRepo=None)],
                    [exact, exact], [exact, dict(exact, branch='wrong')])
        before = git(self.repo, 'show-ref')
        for rows in variants:
            with self.subTest(rows=rows):
                self.registry = copy.deepcopy(rows)
                self.assert_refusal('workspace_mismatch', lambda: self.provider.adopt(self.run, str(path)))
                self.assertEqual(self.registry, rows)
        self.assertEqual(git(self.repo, 'show-ref'), before)
        self.assertEqual(self.add_calls, 0)

    def test_linked_adoption_independently_checks_git_worktree_registration(self):
        path = self.home / 'linked-legacy'
        git(self.repo, 'worktree', 'add', '-b', 'legacy-branch', str(path))
        self.registry = [{'path': str(path), 'parentRepo': str(self.repo), 'branch': 'legacy-branch'}]
        self.run['target'] = None

        def without_git_registration(argv, run, **kwargs):
            if argv == ['git', 'worktree', 'list', '--porcelain', '-z']:
                return ''
            return self.cli(argv, run, **kwargs)
        with patch.object(self.provider, '_command', side_effect=without_git_registration):
            self.assert_refusal('workspace_mismatch', lambda: self.provider.adopt(self.run, str(path)))
        self.assertEqual(self.add_calls, 0)

    def test_adoption_requires_recorded_canonical_path_and_never_selects_registry_candidate(self):
        path = self.home / 'linked-legacy'
        git(self.repo, 'worktree', 'add', '-b', 'legacy-branch', str(path))
        self.registry = [{'path': str(path), 'parentRepo': str(self.repo), 'branch': 'legacy-branch'}]
        self.run['target'] = None
        alias = self.home / 'alias'
        alias.symlink_to(path, target_is_directory=True)
        for value in (None, '', 'linked-legacy', str(alias)):
            with self.subTest(value=value):
                self.assert_refusal('workspace_mismatch', lambda: self.provider.adopt(self.run, value))
        self.assert_refusal('workspace_missing', lambda: self.provider.adopt(self.run, str(self.home / 'missing')))
        self.assert_refusal('workspace_missing', lambda: self.provider.adopt(self.run, str(path / 'source')))
        self.assertEqual(self.add_calls, 0)

    def test_adoption_does_not_relax_issue_branch_checks_for_new_plans(self):
        path = self.home / '.omp/wt/test-1'
        path.parent.mkdir(parents=True)
        git(self.repo, 'worktree', 'add', '-b', 'historical-branch', str(path))
        self.registry = [{'path': str(path), 'parentRepo': str(self.repo), 'branch': 'historical-branch'}]
        workspace = self.provider.adopt(self.run, str(path))
        self.assert_refusal('workspace_mismatch', lambda: self.provider.plan(self.run))
        private = {'plannedWorkspace': workspace}
        self.assert_refusal('workspace_mismatch', lambda: self.provider.recover('workspace', self.run, {}, private))
        self.assertEqual(git(path, 'symbolic-ref', 'HEAD'), 'refs/heads/historical-branch')
        self.assertEqual(self.add_calls, 0)

    def test_adoption_revalidates_canonical_source_not_registry_alone(self):
        path = self.home / 'linked-legacy'
        git(self.repo, 'worktree', 'add', '-b', 'legacy-branch', str(path))
        self.registry = [{'path': str(path), 'parentRepo': str(self.repo), 'branch': 'legacy-branch'}]
        self.run['target'] = None
        self.run['scope']['repository']['commonDir'] = str(self.home)
        self.assert_refusal('repository_mismatch', lambda: self.provider.adopt(self.run, str(path)))
        self.assertEqual(self.add_calls, 0)

    def test_registry_or_backing_identity_changes_are_refused(self):
        workspace, private = self.provision()
        self.registry[0]['orphanReason'] = 'parent repo missing'
        self.assert_refusal('workspace_mismatch', lambda: self.provider.validate(self.run))
        self.registry[0].pop('orphanReason')
        private['plannedWorkspace']['backing'] = 'clone'
        private['plannedWorkspace']['commonDir'] = str(Path(workspace['path']) / '.git')
        self.assert_refusal('workspace_mismatch', lambda: self.provider.recover('workspace', self.run, {}, private))

    def test_settings_changed_after_plan_do_not_change_receipt_or_allocate(self):
        _, private = self.planned()
        self.base_setting = str(self.home / 'changed')
        self.assert_refusal('workspace_mismatch', lambda: self.provider.perform('workspace', self.run, {}, private))
        self.assertEqual(self.add_calls, 0)

    def test_unbound_existing_branch_is_not_reset_or_recreated(self):
        git(self.repo, 'branch', 'test-1')
        self.assert_refusal('workspace_mismatch', lambda: self.provider.plan(self.run))
        self.assertEqual(git(self.repo, 'rev-parse', 'test-1'), self.initial)
        self.assertEqual(self.add_calls, 0)

    def test_missing_target_history_never_fetches_or_allocates(self):
        self.run['target']['ref'] = 'refs/heads/missing'
        self.assert_refusal('evidence_unavailable', lambda: self.provider.plan(self.run))
        self.assertEqual(self.add_calls, 0)
        self.assertFalse(any('fetch' in argv or 'push' in argv or 'merge' in argv for argv in self.calls))

    def test_target_clone_cannot_replace_canonical_source_authority(self):
        self.run['target']['repository'] = {'root': str(self.home / 'clone'), 'commonDir': str(self.home / 'clone/.git')}
        self.assert_refusal('repository_mismatch', lambda: self.provider.plan(self.run))
        self.assertEqual(self.add_calls, 0)

    def test_dangling_workspace_symlink_is_not_followed_for_allocation(self):
        path = self.home / '.omp/wt/test-1'
        path.parent.mkdir(parents=True)
        path.symlink_to(self.home / 'missing')
        self.assert_refusal('workspace_mismatch', lambda: self.provider.plan(self.run))
        self.assertEqual(self.add_calls, 0)

    def test_malformed_registry_fails_closed_before_allocating(self):
        for rows in ({'path': '/not-a-list'}, [{'path': 'relative'}],
                     [{'path': '/absolute', 'parentRepo': 42}], [None]):
            with self.subTest(rows=rows):
                self.registry = rows
                self.assert_refusal('invalid_record', lambda: self.provider.plan(self.run))
        self.assertEqual(self.add_calls, 0)

    def test_invalid_settings_json_fails_closed(self):
        with patch.object(self.provider, '_json', return_value={'key': 'worktree.base', 'value': 42}):
            self.assert_refusal('invalid_record', lambda: self.provider.plan(self.run))
        self.assertEqual(self.add_calls, 0)

    def test_remote_target_provisioning_stays_local(self):
        self.run['target'].update(mode='remote-target', remoteUrl='https://unreachable.invalid/repository')
        workspace, _ = self.provision()
        self.assertEqual(git(workspace['path'], 'rev-parse', 'HEAD'), self.initial)
        self.assertEqual(git(self.repo, 'remote'), '')
        self.assertFalse(any('fetch' in argv or 'push' in argv or 'merge' in argv for argv in self.calls))


if __name__ == '__main__':
    unittest.main()

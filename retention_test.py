"""Retention observations are not removal authority, across modes and Git backings."""
import copy
from datetime import datetime, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import controller_test
from controller_test import git
from beads_contract import Refusal
from beads_controller import Controller
from beads_identity import repository_identity
from beads_service import LifecycleServer
from beads_store import Store
from beads_workspace import OmpWorkspace


class RetentionTests(unittest.TestCase):
    def fixture(self, mode='local-target', backing='linked'):
        fixture = controller_test.LifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.start()
        target = fixture.target if mode == 'local-target' else {
            'mode': 'github-pr', 'repository': {'host': 'github.com', 'repositoryId': '123',
            'owner': 'owner', 'name': 'repo'}, 'ref': 'refs/heads/master'}
        identity = fixture.handoff(target=target)
        if mode == 'local-target':
            git(fixture.repo, 'merge', '--ff-only', identity['head'])
        if backing == 'clone':
            clone = fixture.home / 'independent-clone'
            git(fixture.home, 'clone', str(fixture.workspace.path), str(clone))
            with fixture.store.transaction(fixture.scope) as doc:
                run = doc['runs'][doc['current']]
                run['workspace'] = {**run['workspace'], 'backing': 'clone', 'path': str(clone),
                                    'commonDir': repository_identity(str(clone))['commonDir']}
                doc['private'][doc['current']]['plannedWorkspace'] = copy.deepcopy(run['workspace'])
        run = fixture.status()['run']
        workspace = run['workspace']
        provider = OmpWorkspace()
        registry = [{'path': workspace['path'], 'parentRepo': str(fixture.repo),
                     'branch': 'task', 'kind': 'pr-checkout'}]
        registry_patch = patch.object(provider, '_registry', return_value=registry)
        registry_patch.start()
        self.addCleanup(registry_patch.stop)
        fixture.controller.workspace = provider
        if mode == 'github-pr':
            # Exercise actual PR observer parsing, with only the HTTP response replaced.
            import beads_integration
            record = {'number': 1, 'node_id': 'PR_retention', 'url': 'https://api.github.com/repos/owner/repo/pulls/1',
                      'html_url': identity['pullRequest']['url'], 'state': 'closed', 'merged': True,
                      'merged_at': '2026-11-01T11:59:00Z', 'merge_commit_sha': identity['head'],
                      'head': {'sha': identity['head']}, 'base': {'ref': 'master', 'repo': {
                          'id': 123, 'node_id': 'R_retention', 'owner': {'login': 'owner'}, 'name': 'repo',
                          'html_url': 'https://github.com/owner/repo',
                          'url': 'https://api.github.com/repos/owner/repo'}}}
            original = beads_integration.command
            def command(argv, *args, **kwargs):
                if argv[0] == 'gh':
                    return ('HTTP/2.0 200 OK\r\nDate: ' + format_datetime(datetime.now(timezone.utc), usegmt=True) +
                            '\r\nContent-Type: application/json\r\n\r\n' + json.dumps(record))
                return original(argv, *args, **kwargs)
            http_patch = patch('beads_integration.command', side_effect=command)
            http_patch.start()
            self.addCleanup(http_patch.stop)
        return fixture, provider, registry

    def server(self, fixture):
        config = {'schemaVersion': 1, 'pollSeconds': 30,
                  'socketPath': str(fixture.home / 'retention.sock'),
                  'storeRoot': str(fixture.store.root), 'projects': [],
                  'executables': {'git': shutil.which('git'), 'bd': shutil.which('bd')}}
        return LifecycleServer(fixture.controller, config, fixture.home / 'service-status.json')

    def test_closed_released_retention_survives_restart_in_both_modes_and_backings(self):
        for mode in ('local-target', 'github-pr'):
            for backing in ('linked', 'clone'):
                with self.subTest(mode=mode, backing=backing):
                    fixture, provider, registry = self.fixture(mode, backing)
                    run = fixture.reconcile()
                    self.assertEqual(run['closure']['state'], 'awaiting_manual')
                    self.assertEqual(run['cleanup']['workspaceObservation']['state'], 'present')
                    terminal = copy.deepcopy(run['execution']['terminal'])
                    session = copy.deepcopy(run['execution']['session'])
                    fixture.call('release', {'expected': fixture.condition(), 'reason': 'owner request'})
                    fixture.drain()
                    fixture.authority.row['status'] = 'closed'
                    original_registry = copy.deepcopy(registry)
                    receipts = {path: path.read_bytes() for path in fixture.home.iterdir()
                                if path.is_file() and path.name in fixture.store.read(fixture.scope)['operations']}
                    fixture.controller = Controller('machine', Store(fixture.home / 'state'),
                        fixture.authority, fixture.execution, provider)
                    server = self.server(fixture)
                    server.reconcile_once()
                    server.reconcile_once()
                    row = server.health['runs'][0]
                    self.assertEqual(row['closure']['state'], 'observed_closed')
                    self.assertEqual(row['release']['state'], 'released')
                    self.assertEqual(row['workspace']['backing'], backing)
                    self.assertEqual(row['execution']['terminal'], terminal)
                    self.assertEqual(row['execution']['session'], session)
                    self.assertEqual(row['cleanup']['workspaceObservation']['state'], 'present')
                    self.assertIn('manual_cleanup_required', row['cleanup']['blockers'])
                    self.assertIsNone(row['closure']['manualCommand'])
                    self.assertTrue(Path(row['workspace']['path']).is_dir())
                    self.assertEqual(registry, original_registry)
                    self.assertEqual(fixture.authority.row['metadata'], {'unrelated': {'keep': True}})
                    for path, content in receipts.items():
                        self.assertEqual(path.read_bytes(), content)

    def test_closed_extra_work_remains_visible_after_restart(self):
        fixture, provider, registry = self.fixture()
        fixture.reconcile()
        path = fixture.workspace.path
        (path / 'extra').write_text('post-submission work\n')
        git(path, 'add', 'extra')
        git(path, 'commit', '-m', 'retained extra work')
        extra_head = git(path, 'rev-parse', 'HEAD')
        (path / 'source').write_text('dirty tracked work\n')
        (path / 'untracked').write_text('retained untracked work\n')
        fixture.authority.row['status'] = 'closed'
        self.server(fixture).reconcile_once()
        fixture.controller = Controller('machine', Store(fixture.home / 'state'),
            fixture.authority, fixture.execution, provider)
        self.server(fixture).reconcile_once()
        run = fixture.status()['run']
        self.assertEqual(run['closure']['state'], 'observed_closed')
        self.assertIsNone(run['closure']['manualCommand'])
        self.assertEqual(run['coverage']['state'], 'unknown')
        self.assertIn('unpushed_or_unpreserved', run['cleanup']['blockers'])
        self.assertIn('dirty', run['cleanup']['blockers'])
        self.assertEqual(git(path, 'rev-parse', 'HEAD'), extra_head)
        self.assertEqual((path / 'source').read_text(), 'dirty tracked work\n')
        self.assertEqual((path / 'untracked').read_text(), 'retained untracked work\n')
        self.assertEqual(registry[0]['path'], str(path))

    def test_released_run_recovers_reopen_and_reassignment_without_reclosure(self):
        for change, disposition in (({'status': 'in_progress'}, 'reopened'),
                                    ({'assignee': 'new-owner'}, 'reassigned')):
            with self.subTest(disposition=disposition):
                fixture, provider, registry = self.fixture()
                fixture.reconcile()
                fixture.call('release', {'expected': fixture.condition(), 'reason': 'owner release'})
                fixture.drain()
                fixture.authority.row['status'] = 'closed'
                fixture.reconcile()
                released = copy.deepcopy(fixture.status()['run']['release'])
                fixture.authority.row.update(change)
                fixture.controller = Controller('machine', Store(fixture.home / 'state'),
                    fixture.authority, fixture.execution, provider)
                server = self.server(fixture)
                server.reconcile_once()
                server.reconcile_once()
                run = fixture.status()['run']
                self.assertEqual(run['disposition'], disposition)
                self.assertEqual(run['release'], released)
                self.assertEqual(run['execution']['state'], 'released')
                self.assertEqual(run['execution']['activity'], 'inactive')
                self.assertIsNone(run['closure']['manualCommand'])
                self.assertEqual(fixture.authority.row['status'], change.get('status', 'closed'))
                self.assertIn('manual_cleanup_required', run['cleanup']['blockers'])
                self.assertTrue(Path(run['workspace']['path']).is_dir())
                self.assertEqual(registry[0]['path'], run['workspace']['path'])

    def test_missing_workspace_still_records_external_closure_and_keeps_registration(self):
        fixture, provider, registry = self.fixture()
        fixture.reconcile()
        recorded = fixture.status()['run']['workspace']
        fixture.workspace.path.rename(fixture.home / 'externally-moved')
        fixture.authority.row['status'] = 'closed'
        server = self.server(fixture)
        server.reconcile_once()
        server.reconcile_once()
        run = fixture.status()['run']
        self.assertEqual(run['closure']['state'], 'observed_closed')
        self.assertEqual(run['issueObservation']['status'], 'closed')
        self.assertEqual(run['cleanup']['workspaceObservation']['state'], 'missing')
        self.assertEqual(run['workspace'], recorded)
        self.assertIsNone(run['closure']['manualCommand'])
        self.assertIn(recorded['path'], git(fixture.repo, 'worktree', 'list', '--porcelain'))
        self.assertEqual(registry[0]['path'], recorded['path'])
        fixture.controller = Controller('machine', Store(fixture.home / 'state'), fixture.authority)
        self.assertEqual(fixture.status()['run']['cleanup']['workspaceObservation']['state'], 'missing')

    def test_unknown_capability_and_unavailable_issue_keep_observations_inspectable(self):
        fixture, _, _ = self.fixture()
        fixture.controller.workspace = None
        fixture.reconcile()
        run = fixture.status()['run']
        self.assertEqual(run['cleanup']['workspaceObservation']['state'], 'unknown')
        with patch.object(fixture.authority, 'issue', side_effect=Refusal('evidence_unavailable', 'offline database')):
            self.server(fixture).reconcile_once()
        run = fixture.status()['run']
        self.assertEqual(run['cleanup']['state'], 'held')
        self.assertIsNone(run['closure']['manualCommand'])
        self.assertTrue(fixture.workspace.path.exists())

    def test_risky_work_and_changed_identity_never_trigger_removal(self):
        fixture, _, registry = self.fixture()
        fixture.reconcile()
        path = fixture.workspace.path
        (path / 'source').write_text('stashed work\n')
        git(path, 'stash', 'push', '-m', 'retained stash')
        (path / '.gitignore').write_text('.secret\n')
        git(path, 'add', '.gitignore')
        git(path, 'commit', '-m', 'additional unpreserved commit')
        extra_head = git(path, 'rev-parse', 'HEAD')
        (path / 'source').write_text('dirty tracked work\n')
        (path / 'untracked').write_text('untracked work\n')
        (path / '.secret').write_text('ignored work\n')
        nested = path / 'nested'
        nested.mkdir()
        git(nested, 'init', '-b', 'nested')
        original_registry = copy.deepcopy(registry)
        server = self.server(fixture)
        server.reconcile_once()
        fixture.authority.row.update(status='closed', assignee='new-owner')
        git(path, 'checkout', '-b', 'changed-identity')
        server.reconcile_once()
        server.reconcile_once()
        run = fixture.status()['run']
        self.assertEqual(run['cleanup']['workspaceObservation']['state'], 'unknown')
        self.assertEqual(run['release']['state'], 'held')
        self.assertEqual(run['closure']['state'], 'observed_closed')
        self.assertEqual(git(path, 'rev-parse', 'HEAD'), extra_head)
        self.assertIn('retained stash', git(path, 'stash', 'list'))
        self.assertEqual((path / 'source').read_text(), 'dirty tracked work\n')
        self.assertEqual((path / 'untracked').read_text(), 'untracked work\n')
        self.assertEqual((path / '.secret').read_text(), 'ignored work\n')
        self.assertTrue((nested / '.git').is_dir())
        self.assertEqual(registry, original_registry)
        self.assertIn(str(path), git(fixture.repo, 'worktree', 'list', '--porcelain'))


if __name__ == '__main__':
    unittest.main()

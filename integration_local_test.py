"""Authoritative target proofs against real disposable repositories, without providers."""
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'bin'))
from beads_contract import Refusal, now, validate
from beads_identity import repository_identity
from beads_integration import IntegrationObserver
import beads_integration


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.PIPE).strip()


class TargetEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.repo = self.home / 'canonical'
        self.repo.mkdir()
        git(self.repo, 'init', '-b', 'master')
        git(self.repo, 'config', 'user.name', 'test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        self.base = self.commit(self.repo, 'base', 'base')
        git(self.repo, 'checkout', '-b', 'source')
        self.head = self.commit(self.repo, 'change', 'submitted')
        git(self.repo, 'checkout', 'master')
        self.repository = repository_identity(str(self.repo))
        self.target = {'mode': 'local-target', 'repository': self.repository, 'ref': 'refs/heads/master'}
        self.identity = {'submissionId': 'exact-source', 'submissionRevision': 1, 'head': self.head,
                         'target': self.target, 'pullRequest': None}
        self.key = {'submissionId': 'exact-source', 'submissionRevision': 1}
        self.run = {'fence': {'runId': 'run', 'attempt': 1, 'generation': 1}, 'owner': 'owner',
                    'scope': {'repository': self.repository}, 'target': self.target,
                    'coverage': {'required': [self.key]},
                    'submissions': [{'identity': self.identity}]}
        self.observer = IntegrationObserver()

    def commit(self, root, name, content):
        (root / name).write_text(content)
        git(root, 'add', name)
        git(root, 'commit', '-m', content)
        return git(root, 'rev-parse', 'HEAD')

    def observe(self, receipts=None):
        result = self.observer.observe(self.run, receipts or [])[0]
        validate('run', result, 'Submission')
        return result

    def receipt(self, result):
        return {'receiptId': 'trusted', 'fence': copy.deepcopy(self.run['fence']), 'submission': self.key,
                'head': self.head, 'target': copy.deepcopy(self.target), 'resultCommit': result,
                'attestedBy': 'owner', 'attestedAt': now()}

    def test_normal_and_fast_forward_merge_pin_canonical_commit_offline(self):
        for strategy in ('--ff-only', '--no-ff'):
            with self.subTest(strategy=strategy):
                git(self.repo, 'reset', '--hard', self.base)
                git(self.repo, 'merge', strategy, '-m', 'integration', self.head)
                with patch.dict(os.environ, {'GH_TOKEN': '', 'GH_HOST': 'unavailable.invalid'}):
                    observed = self.observe()
                self.assertEqual(git(self.repo, 'remote'), '')
                self.assertEqual(observed['state'], 'integrated')
                self.assertEqual(observed['evidence']['targetCommit'], git(self.repo, 'rev-parse', 'master'))
                self.assertEqual(observed['evidence']['head'], self.head)
                self.assertEqual(observed['evidence']['proof'], 'ancestry')
                self.assertEqual(observed['evidence']['target'], self.target)

    def test_unmerged_and_deleted_source_are_not_evidence(self):
        git(self.repo, 'branch', '-D', 'source')
        observed = self.observe()
        self.assertEqual(observed['state'], 'needs_confirmation')
        self.assertIsNone(observed['evidence'])

    def test_clone_only_target_is_not_canonical_authority(self):
        clone = self.home / 'clone'
        git(self.home, 'clone', str(self.repo), str(clone))
        git(clone, 'merge', '--ff-only', self.head)
        self.assertIsNone(self.observe()['evidence'])
        self.identity['target'] = {**self.target, 'repository': repository_identity(str(clone))}
        self.run['target'] = self.identity['target']
        with self.assertRaises(Refusal):
            self.observe()

    def test_changed_git_common_directory_refuses_recorded_repository(self):
        self.run['scope']['repository'] = {**self.repository, 'commonDir': str(self.home / 'wrong.git')}
        self.identity['target'] = {**self.target, 'repository': self.run['scope']['repository']}
        self.run['target'] = self.identity['target']
        with self.assertRaises(Refusal):
            self.observe()

    def test_wrong_or_missing_full_ref_never_uses_source_branch(self):
        self.identity['target'] = {**self.target, 'ref': 'source'}
        with self.assertRaises(Refusal):
            self.observe()
        self.identity['target'] = {**self.target, 'ref': 'refs/heads/missing'}
        with self.assertRaises(Refusal):
            self.observe()

    def test_target_reset_during_ancestry_invalidates_pinned_proof(self):
        git(self.repo, 'merge', '--ff-only', self.head)
        original = beads_integration.ancestor
        def reset_after_proof(*args):
            result = original(*args)
            git(self.repo, 'reset', '--hard', self.base)
            return result
        with patch.object(beads_integration, 'ancestor', reset_after_proof):
            with self.assertRaises(Refusal):
                self.observe()

    def test_replace_refs_cannot_forge_source_ancestry(self):
        # Replacement commit claims the source as parent although master does not integrate it.
        tree = git(self.repo, 'rev-parse', self.base + '^{tree}')
        replacement = git(self.repo, 'commit-tree', tree, '-p', self.head, '-m', 'forged parent')
        git(self.repo, 'replace', self.base, replacement)
        self.assertIsNone(self.observe()['evidence'])

    def test_rewritten_history_requires_exact_owner_receipt(self):
        git(self.repo, 'merge', '--squash', self.head)
        git(self.repo, 'commit', '-m', 'rewritten')
        result = git(self.repo, 'rev-parse', 'HEAD')
        self.assertEqual(self.observe()['state'], 'needs_confirmation')
        receipt = self.receipt(result)
        for field, value in [('head', self.base), ('submission', {**self.key, 'submissionRevision': 2}),
                             ('attestedBy', 'another-owner'), ('fence', {**self.run['fence'], 'generation': 2}),
                             ('target', {**self.target, 'ref': 'refs/heads/other'})]:
            with self.subTest(field=field):
                wrong = {**receipt, field: value}
                self.assertIsNone(self.observe([wrong])['evidence'])
        observed = self.observe([receipt])
        self.assertEqual(observed['state'], 'integrated')
        self.assertEqual(observed['evidence']['receipt'], receipt)
        git(self.repo, 'reset', '--hard', self.base)
        self.assertIsNone(self.observe([receipt])['evidence'])

    def test_remote_tracking_ref_and_local_integration_are_not_publication(self):
        remote = self.home / 'published.git'
        git(self.home, 'clone', '--bare', str(self.repo), str(remote))
        self.target.update(mode='remote-target', remoteUrl=str(remote))
        git(self.repo, 'merge', '--ff-only', self.head)
        git(self.repo, 'update-ref', 'refs/remotes/origin/master', self.head)
        self.assertIsNone(self.observe()['evidence'])
        git(remote, 'update-ref', 'refs/heads/master', self.head)
        evidence = self.observe()['evidence']
        self.assertEqual(evidence['targetCommit'], self.head)
        self.assertEqual(evidence['authorityUrl'], str(remote))
        git(remote, 'update-ref', 'refs/heads/master', self.base)
        self.assertIsNone(self.observe()['evidence'])

    def test_local_cherry_pick_and_rebase_require_mapping(self):
        target_base = self.commit(self.repo, 'target-work', 'independent target work')
        git(self.repo, 'cherry-pick', self.head)
        self.assertEqual(self.observe()['state'], 'needs_confirmation')
        git(self.repo, 'reset', '--hard', target_base)
        git(self.repo, 'checkout', 'source')
        git(self.repo, 'rebase', 'master')
        rewritten = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'checkout', 'master')
        git(self.repo, 'merge', '--ff-only', rewritten)
        self.assertEqual(self.observe()['state'], 'needs_confirmation')
        self.assertEqual(self.observe([self.receipt(rewritten)])['state'], 'integrated')

    def test_symbolic_target_cannot_redirect_authority_to_feature_branch(self):
        git(self.repo, 'symbolic-ref', 'refs/heads/master', 'refs/heads/source')
        with self.assertRaises(Refusal):
            self.observe()

    def test_remote_missing_objects_or_authority_never_falls_back(self):
        remote = self.home / 'published.git'
        git(self.home, 'clone', '--bare', str(self.repo), str(remote))
        other = self.home / 'publisher'
        git(self.home, 'clone', str(remote), str(other))
        git(other, 'config', 'user.name', 'test')
        git(other, 'config', 'user.email', 'test@example.invalid')
        unpublished_object = self.commit(other, 'unknown', 'remote-only')
        git(remote, 'fetch', str(other), 'master:master')
        self.target.update(mode='remote-target', remoteUrl=str(remote))
        with self.assertRaises(Refusal):
            self.observe()
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.repo, 'cat-file', '-e', unpublished_object)
        self.target['remoteUrl'] = str(self.home / 'unavailable.git')
        git(self.repo, 'merge', '--ff-only', self.head)
        with self.assertRaises(Refusal):
            self.observe()


if __name__ == '__main__':
    unittest.main()

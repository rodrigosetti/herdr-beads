"""Deterministic GitHub authority regressions; no GitHub or Git mutation."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'bin'))
from beads_contract import Refusal, validate
from beads_integration import IntegrationObserver


OBSERVED_AT = '2026-11-01T12:00:00Z'
RESPONSE_DATE = 'Sun, 01 Nov 2026 12:00:00 GMT'


class PullRequestTests(unittest.TestCase):
    def setUp(self):
        self.target = {'mode': 'github-pr', 'repository': {
            'host': 'github.com', 'repositoryId': '123', 'owner': 'owner', 'name': 'repo'},
            'ref': 'refs/heads/main'}
        self.pr = {'number': 17, 'url': 'https://github.com/owner/repo/pull/17'}
        self.identity = {'submissionId': 'submission', 'submissionRevision': 1,
                         'head': 'a' * 40, 'target': copy.deepcopy(self.target),
                         'pullRequest': copy.deepcopy(self.pr)}
        self.record = {
            'number': 17, 'node_id': 'PR_exact',
            'url': 'https://api.github.com/repos/owner/repo/pulls/17',
            'html_url': self.pr['url'], 'state': 'closed', 'merged': True,
            'merged_at': '2026-11-01T11:59:00Z', 'merge_commit_sha': 'b' * 40,
            'head': {'sha': self.identity['head'], 'repo': None},
            'base': {'ref': 'main', 'repo': {
                'id': 123, 'node_id': 'R_exact', 'owner': {'login': 'owner'},
                'name': 'repo', 'html_url': 'https://github.com/owner/repo',
                'url': 'https://api.github.com/repos/owner/repo'}},
        }

    def observe(self, record=None, *, headers=None, status='HTTP/2.0 200 OK', body=None):
        record = self.record if record is None else record
        payload = json.dumps(record) if body is None else body
        response_headers = {'Date': RESPONSE_DATE, 'Content-Type': 'application/json; charset=utf-8'}
        if headers:
            response_headers.update(headers)
        wire = status + '\r\n' + '\r\n'.join(
            f'{key}: {value}' for key, value in response_headers.items()) + '\r\n\r\n' + payload

        def provider(argv, cwd, **kwargs):
            # The pre-fix implementation asks for JSON only. Keeping this boundary
            # faithful lets the open-but-merged regression expose its false proof.
            return wire if '--include' in argv else payload

        with patch('beads_integration.command', side_effect=provider) as request, \
                patch('beads_integration.now', return_value=OBSERVED_AT), \
                patch('beads_integration.ancestor', side_effect=AssertionError('no Git fallback')):
            result = IntegrationObserver.pull_request(self.target, self.identity, '/repository')
        self.assertEqual(request.call_count, 1)
        return result, request.call_args

    def assert_refused(self, record=None, **kwargs):
        with self.assertRaises(Refusal):
            self.observe(record, **kwargs)

    def test_exact_merged_record_persists_current_schema_proof(self):
        (state, detail), _ = self.observe()
        self.assertEqual(state, 'integrated')
        self.assertEqual(detail, {'targetCommit': 'b' * 40, 'pullRequest': self.pr,
                                 'mergedAt': self.record['merged_at'], 'mergedHead': 'a' * 40,
                                 'mergeCommit': 'b' * 40, 'providerRecordId': 'PR_exact'})
        validate('run', dict(detail, evidenceId='proof', fence={'runId': 'run', 'generation': 1,
                 'attempt': 1}, submission={'submissionId': 'submission', 'submissionRevision': 1},
                 head=self.identity['head'], observedAt=OBSERVED_AT, kind='github-pr', target=self.target),
                 'PullRequestEvidence')

    def test_deleted_fork_head_repository_is_not_required(self):
        self.assertIsNone(self.record['head']['repo'])
        self.assertEqual(self.observe()[0][0], 'integrated')
        del self.record['head']['repo']
        self.assertEqual(self.observe()[0][0], 'integrated')

    def test_repository_node_id_is_a_supported_stable_identity(self):
        self.target['repository']['repositoryId'] = 'R_exact'
        self.identity['target'] = copy.deepcopy(self.target)
        self.assertEqual(self.observe()[0][0], 'integrated')

    def test_open_record_with_merged_true_is_not_proof_regression(self):
        # Previously integrated: merged=true and a nonempty timestamp sufficed,
        # even when the exact same authoritative record still said state=open.
        self.record['state'] = 'open'
        self.assert_refused()

    def test_wrong_repository_identity_base_number_or_urls_refuse(self):
        mutations = [
            ('base.repo.id', 999), ('base.repo.owner.login', 'other'),
            ('base.repo.name', 'other'), ('base.ref', 'release'), ('number', 18),
            ('html_url', 'https://evil.example/owner/repo/pull/17'),
            ('url', 'https://api.github.com/repos/other/repo/pulls/17'),
            ('base.repo.html_url', 'https://evil.example/owner/repo'),
            ('base.repo.url', 'https://evil.example/repos/owner/repo'),
        ]
        for path, wrong in mutations:
            with self.subTest(path=path):
                record = copy.deepcopy(self.record)
                parent = record
                names = path.split('.')
                for name in names[:-1]:
                    parent = parent[name]
                parent[names[-1]] = wrong
                self.assert_refused(record)

    def test_matching_forged_pr_url_does_not_override_repository_host(self):
        self.pr['url'] = 'https://evil.example/owner/repo/pull/17'
        self.identity['pullRequest'] = copy.deepcopy(self.pr)
        self.record['html_url'] = self.pr['url']
        self.assert_refused()

    def test_wrong_source_head_holds_even_when_provider_says_merged(self):
        self.record['head']['sha'] = 'c' * 40
        self.assertEqual(self.observe()[0], ('head_changed', None))

    def test_closed_unmerged_and_auto_merge_are_not_proof(self):
        self.record.update(merged=False, merged_at=None)
        self.record['body'] = 'Merged, approved, and integrated. Fixes task.'
        self.assertEqual(self.observe()[0], ('closed_unmerged', None))
        self.record.update(state='open', auto_merge={'enabled_by': {'login': 'owner'}})
        self.assertEqual(self.observe()[0], ('open', None))

    def test_invalid_or_future_merge_times_refuse(self):
        for timestamp in [None, '', 'not-a-time', '2026-02-30T00:00:00Z',
                          '2026-11-01T12:00:01Z', '2026-11-01T11:59:00', 42]:
            with self.subTest(timestamp=timestamp):
                self.record['merged_at'] = timestamp
                self.assert_refused()

    def test_malformed_and_contradictory_provider_records_refuse(self):
        for path, wrong in [('merged', 1), ('merged', 'true'), ('state', 'merged'),
                            ('head', None), ('head', {'sha': True}), ('base', None),
                            ('node_id', None), ('node_id', ''), ('number', True),
                            ('merge_commit_sha', 'not-a-sha')]:
            with self.subTest(path=path, wrong=wrong):
                record = copy.deepcopy(self.record)
                record[path] = wrong
                self.assert_refused(record)
        self.record['merged'] = False
        self.assert_refused()
        for body in ['[]', 'null', '{}', '{broken']:
            with self.subTest(body=body):
                self.assert_refused(body=body)

    def test_missing_required_authority_fields_refuse(self):
        for field in ['merged', 'merged_at', 'state', 'node_id', 'head', 'base', 'url', 'html_url']:
            with self.subTest(field=field):
                record = copy.deepcopy(self.record)
                del record[field]
                self.assert_refused(record)

    def test_detectably_stale_or_ambiguous_http_responses_refuse(self):
        for headers in [{'Date': 'Sun, 01 Nov 2026 11:58:59 GMT'},
                        {'Date': 'Sun, 01 Nov 2026 12:01:01 GMT'}, {'Date': 'invalid'},
                        {'Age': '1'}, {'Age': '-1'}, {'Age': 'invalid'},
                        {'Warning': '110 api.github.com "Response is stale"'},
                        {'X-Cache': 'HIT'}, {'date': RESPONSE_DATE}]:
            with self.subTest(headers=headers):
                self.assert_refused(headers=headers)
        for status in ['HTTP/2.0 304 Not Modified', 'HTTP/2.0 401 Unauthorized',
                       'HTTP/2.0 302 Found', 'HTTP/2.0 500 Server Error']:
            with self.subTest(status=status):
                self.assert_refused(status=status)

    def test_duplicate_json_authority_fields_refuse(self):
        body = json.dumps(self.record)
        self.assert_refused(body=body[:-1] + ', "merged": false}')
        self.assert_refused(body=body.replace('"state": "closed"', '"state": "open", "state": "closed"'))

    def test_wrong_target_or_malformed_explicit_identity_refuse(self):
        self.identity['target']['ref'] = 'refs/heads/other'
        self.assert_refused()
        self.identity['target'] = copy.deepcopy(self.target)
        self.target['repository']['host'] = 'github.com/evil'
        self.identity['target'] = copy.deepcopy(self.target)
        self.assert_refused()

    def test_authentication_and_network_errors_propagate_without_fallback(self):
        for message in ['HTTP 401: Bad credentials', 'network timeout', 'rate limit exceeded']:
            with self.subTest(message=message):
                error = Refusal('evidence_unavailable', message, True)
                with patch('beads_integration.command', side_effect=error) as request, \
                        patch('beads_integration.ancestor', side_effect=AssertionError('no fallback')):
                    with self.assertRaises(Refusal) as raised:
                        IntegrationObserver.pull_request(self.target, self.identity, '/repository')
                self.assertIs(raised.exception, error)
                self.assertEqual(request.call_count, 1)

    def test_enterprise_host_is_bound_to_web_and_api_identity(self):
        self.target['repository']['host'] = 'github.example.com'
        self.identity['target'] = copy.deepcopy(self.target)
        self.identity['pullRequest']['url'] = 'https://github.example.com/owner/repo/pull/17'
        self.record['html_url'] = self.identity['pullRequest']['url']
        self.record['url'] = 'https://github.example.com/api/v3/repos/owner/repo/pulls/17'
        self.record['base']['repo'].update(html_url='https://github.example.com/owner/repo',
                                         url='https://github.example.com/api/v3/repos/owner/repo')
        self.assertEqual(self.observe()[0][0], 'integrated')
        self.record['base']['repo']['url'] = 'https://api.github.com/repos/owner/repo'
        self.assert_refused()


if __name__ == '__main__':
    unittest.main()

"""Read-only Git/GitHub evidence. Never fetch, push, merge, close or remove."""
import copy
import json
import os
import re
import subprocess
import uuid

from beads_contract import Refusal, now
from beads_identity import command


def ancestor(repository, source, target):
    # Missing objects are unavailable evidence, not a normal non-ancestor result.
    command(['git', 'cat-file', '-e', source + '^{commit}'], repository)
    command(['git', 'cat-file', '-e', target + '^{commit}'], repository)
    try:
        result = subprocess.run(['git', 'merge-base', '--is-ancestor', source, target],
                                cwd=repository, capture_output=True, text=True, timeout=3,
                                env={k: v for k, v in os.environ.items() if not k.startswith('GIT_')})
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refusal('evidence_unavailable', f'Cannot observe Git ancestry: {error}', True) from error
    if result.returncode not in (0, 1):
        raise Refusal('evidence_unavailable', f'Git ancestry unavailable: {result.stderr.strip()[:500]}', True)
    return result.returncode == 0


class IntegrationObserver:
    def observe(self, run, receipts):
        result = []
        for submission in run['submissions']:
            identity = submission['identity']
            submission_key = {name: identity[name] for name in ('submissionId', 'submissionRevision')}
            if submission_key not in run['coverage']['required']:
                continue
            target = identity['target']
            evidence = {'evidenceId': str(uuid.uuid4()), 'fence': copy.deepcopy(run['fence']),
                        'submission': submission_key, 'head': identity['head'], 'observedAt': now(),
                        'kind': target['mode'], 'target': copy.deepcopy(target)}
            state = 'pending'
            if target['mode'] == 'github-pr':
                state, detail = self.pull_request(target, identity, run['scope']['repository']['root'])
                if state == 'integrated':
                    evidence.update(detail)
            else:
                repository = target['repository']['root']
                if target['mode'] == 'local-target':
                    target_commit = command(['git', 'rev-parse', '--verify', target['ref'] + '^{commit}'], repository)
                else:
                    observed = command(['git', 'ls-remote', '--exit-code', '--refs', target['remoteUrl'], target['ref']], repository, timeout=10).splitlines()
                    if len(observed) != 1 or observed[0].split()[1] != target['ref']:
                        raise Refusal('evidence_unavailable', 'Remote authority did not return exactly the intended full ref.', True)
                    target_commit = observed[0].split()[0]
                evidence['targetCommit'] = target_commit
                if ancestor(repository, identity['head'], target_commit):
                    state = 'integrated'
                    if target['mode'] == 'local-target':
                        evidence.update(proof='ancestry', receipt=None)
                elif target['mode'] == 'local-target':
                    state = 'needs_confirmation'
                    mappings = [r for r in receipts if r['fence'] == run['fence'] and r['submission'] == submission_key
                                and r['head'] == identity['head'] and r['target'] == target and r['attestedBy'] == run['owner']]
                    for receipt in mappings:
                        if ancestor(repository, receipt['resultCommit'], target_commit):
                            state = 'integrated'
                            evidence.update(proof='receipt', receipt=copy.deepcopy(receipt))
                            break
                if target['mode'] == 'remote-target':
                    evidence.update(authorityUrl=target['remoteUrl'], observedRemoteRef=target['ref'])
            result.append({'identity': copy.deepcopy(identity), 'state': state,
                           'observedAt': now(), 'evidence': evidence if state == 'integrated' else None})
        return result

    @staticmethod
    def pull_request(target, identity, cwd):
        repository = target['repository']
        if not re.fullmatch(r'[A-Za-z0-9.-]+', repository['host']) or any(
            not re.fullmatch(r'[A-Za-z0-9_.-]+', repository[name]) for name in ('owner', 'name')):
            raise Refusal('evidence_invalid', 'Invalid explicit GitHub repository identity.')
        pr = identity['pullRequest']
        endpoint = f"repos/{repository['owner']}/{repository['name']}/pulls/{pr['number']}"
        try:
            value = json.loads(command(['gh', 'api', '--hostname', repository['host'], endpoint], cwd, timeout=10))
            base = value['base']
            base_repo = base['repo']
            ids = {str(base_repo['id']), str(base_repo['node_id'])}
            if (repository['repositoryId'] not in ids or base_repo['owner']['login'].lower() != repository['owner'].lower()
                    or base_repo['name'].lower() != repository['name'].lower()
                    or 'refs/heads/' + base['ref'] != target['ref'] or value['number'] != pr['number']
                    or value['html_url'] != pr['url']):
                raise Refusal('evidence_invalid', 'GitHub record differs from the exact intended repository/PR/base.')
            if value['head']['sha'] != identity['head']:
                return 'head_changed', None
            if value.get('merged') is not True or not value.get('merged_at'):
                return ('closed_unmerged' if value['state'] == 'closed' else 'open'), None
            sha = value.get('merge_commit_sha')
            if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha):
                raise Refusal('evidence_invalid', 'Merged PR lacks an exact provider merge commit.')
            return 'integrated', {'targetCommit': sha, 'pullRequest': copy.deepcopy(pr),
                                  'mergedAt': value['merged_at'], 'mergedHead': value['head']['sha'],
                                  'mergeCommit': sha, 'providerRecordId': str(value['node_id'])}
        except (ValueError, KeyError, TypeError) as error:
            raise Refusal('evidence_unavailable', 'GitHub returned an incomplete or malformed authoritative record.', True) from error

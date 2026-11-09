"""Read-only Git/GitHub evidence. Never fetch, push, merge, close or remove."""
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import copy
import json
import os
import re
import subprocess
import uuid

from beads_contract import Refusal, now, validate
from beads_identity import canonical_path, command, repository_identity


def ancestor(repository, source, target):
    # Missing objects are unavailable evidence, not a normal non-ancestor result.
    command(['git', 'cat-file', '-e', source + '^{commit}'], repository)
    command(['git', 'cat-file', '-e', target + '^{commit}'], repository)
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_NO_REPLACE_OBJECTS='1', GIT_GRAFT_FILE=os.devnull, GIT_NO_LAZY_FETCH='1')
    try:
        result = subprocess.run(['git', 'merge-base', '--is-ancestor', source, target],
                                cwd=repository, capture_output=True, text=True, timeout=3,
                                env=env)
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
            validate('run', identity, 'SubmissionIdentity')
            if identity['target'] != run['target']:
                raise Refusal('evidence_invalid', 'Submission differs from the explicitly recorded current target.')
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
                target_commit = self.target_commit(target, run['scope']['repository'])
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
        # Recheck all pinned Git targets after ancestry/receipt and any provider IO.
        # No cached local/tracking ref substitutes for this authority observation.
        for submission in result:
            evidence = submission['evidence']
            if evidence and evidence['kind'] != 'github-pr' and self.target_commit(
                    evidence['target'], run['scope']['repository']) != evidence['targetCommit']:
                raise Refusal('evidence_unavailable', 'Authoritative target moved during verification; reconcile again.', True)
        return result

    @staticmethod
    def target_commit(target, canonical_repository):
        repository = target['repository']
        if repository != canonical_repository:
            raise Refusal('repository_mismatch', 'Target must name the recorded canonical repository, not a workspace clone.')
        for path in repository.values():
            canonical_path(path, 'repository_mismatch')
        root = repository['root']
        if repository_identity(root) != repository:
            raise Refusal('repository_mismatch', 'Canonical target Git identity changed.')
        ref = target['ref']
        if not ref.startswith('refs/heads/'):
            raise Refusal('evidence_invalid', 'Target requires an explicit full branch ref.')
        command(['git', 'check-ref-format', ref], root)
        if target['mode'] == 'local-target':
            rows = command(['git', 'for-each-ref', '--format=%(refname)%09%(objectname)%09%(objecttype)%09%(symref)',
                            '--', ref], root).splitlines()
            fields = rows[0].split('\t') if len(rows) == 1 else []
            # command strips trailing whitespace, so a direct ref has three fields.
            if len(fields) != 3 or fields[0] != ref or fields[2] != 'commit':
                raise Refusal('evidence_unavailable', 'Canonical target is missing, symbolic, ambiguous, or not a commit.', True)
            sha = fields[1]
        else:
            url = target['remoteUrl']
            if command(['git', 'ls-remote', '--get-url', '--', url], root) != url:
                raise Refusal('evidence_invalid', 'Remote authority must be an explicit URL/path, not a mutable alias or URL rewrite.')
            rows = command(['git', 'ls-remote', '--exit-code', '--refs', '--', url, ref], root, timeout=10).splitlines()
            fields = rows[0].split() if len(rows) == 1 else []
            if len(fields) != 2 or fields[1] != ref:
                raise Refusal('evidence_unavailable', 'Remote authority did not return exactly the intended full ref.', True)
            sha = fields[0]
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha):
            raise Refusal('evidence_invalid', 'Authority did not return an exact target commit SHA.')
        return sha

    @staticmethod
    def pull_request(target, identity, cwd):
        try:
            repository = target['repository']
            host = repository['host']
            if (not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*', host)
                    or any(not isinstance(repository[name], str)
                           or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*', repository[name])
                           or repository[name] in ('.', '..') for name in ('owner', 'name'))):
                raise Refusal('evidence_invalid', 'Invalid explicit GitHub repository identity.')
            pr = identity['pullRequest']
            if (type(pr['number']) is not int or pr['number'] < 1 or identity['target'] != target
                    or not isinstance(repository['repositoryId'], str) or not repository['repositoryId']):
                raise Refusal('evidence_invalid', 'Invalid exact GitHub submission identity.')
            endpoint = f"repos/{repository['owner']}/{repository['name']}/pulls/{pr['number']}"
            requested_at = datetime.fromisoformat(now().replace('Z', '+00:00'))
            # Include headers to reject detectable cache reuse, not merely request a refresh.
            response = command(['gh', 'api', '--hostname', host, '--method', 'GET', '--include',
                                '--header', 'Accept: application/vnd.github+json',
                                '--header', 'X-GitHub-Api-Version: 2022-11-28',
                                '--header', 'Cache-Control: no-cache, no-store',
                                '--header', 'Pragma: no-cache', endpoint], cwd, timeout=10)
            observed_at = datetime.fromisoformat(now().replace('Z', '+00:00'))
            header_block, body = response.replace('\r\n', '\n').split('\n\n', 1)
            status, *lines = header_block.splitlines()
            if not re.fullmatch(r'HTTP/\d+(?:\.\d+)? 200(?: .*)?', status):
                raise ValueError('not a fresh successful response')
            headers = {}
            for line in lines:
                key, value = line.split(':', 1)
                key = key.strip().lower()
                if not key or key in headers:
                    raise ValueError('ambiguous response headers')
                headers[key] = value.strip()
            response_date = parsedate_to_datetime(headers['date'])
            if (response_date.tzinfo is None
                    or response_date < requested_at - timedelta(seconds=60)
                    or response_date > observed_at + timedelta(seconds=60)
                    or ('age' in headers and (not headers['age'].isdigit() or int(headers['age']) != 0))
                    or re.search(r'(?:^|,)\s*11[0-3]\b', headers.get('warning', ''))
                    or re.search(r'\b(?:HIT|STALE)\b', headers.get('x-cache', ''), re.IGNORECASE)):
                raise ValueError('stale or invalid response freshness')
            def unique_object(pairs):
                result = {}
                for key, item in pairs:
                    if key in result:
                        raise ValueError('ambiguous duplicate JSON field')
                    result[key] = item
                return result

            value = json.loads(body, object_pairs_hook=unique_object)
            base = value['base']
            base_repo = base['repo']
            owner = base_repo['owner']['login']
            name = base_repo['name']
            repo_url = f'https://{host}/{owner}/{name}'
            api_root = 'https://api.github.com' if host.lower() == 'github.com' else f'https://{host}/api/v3'
            api_repo_url = f'{api_root}/repos/{owner}/{name}'
            if (type(base_repo['id']) is not int or base_repo['id'] < 1
                    or not isinstance(base_repo['node_id'], str) or not base_repo['node_id']
                    or not isinstance(value['node_id'], str) or not value['node_id']
                    or type(value['number']) is not int
                    or not isinstance(value['head']['sha'], str)
                    or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value['head']['sha'])
                    or type(value['merged']) is not bool or value['state'] not in ('open', 'closed')):
                raise ValueError('malformed provider identity or merge state')
            ids = {str(base_repo['id']), base_repo['node_id']}
            if (repository['repositoryId'] not in ids or owner.lower() != repository['owner'].lower()
                    or name.lower() != repository['name'].lower()
                    or base_repo['html_url'] != repo_url or base_repo['url'] != api_repo_url
                    or 'refs/heads/' + base['ref'] != target['ref'] or value['number'] != pr['number']
                    or value['html_url'] != pr['url']
                    or value['html_url'] != f"{repo_url}/pull/{pr['number']}"
                    or value['url'] != f"{api_repo_url}/pulls/{pr['number']}"):
                raise Refusal('evidence_invalid', 'GitHub record differs from the exact intended repository/host/PR/base.')
            if value['head']['sha'] != identity['head']:
                return 'head_changed', None
            if not value['merged']:
                if value['merged_at'] is not None:
                    raise ValueError('unmerged record claims a merge time')
                return ('closed_unmerged' if value['state'] == 'closed' else 'open'), None
            merged_at = value['merged_at']
            if (value['state'] != 'closed' or not isinstance(merged_at, str)
                    or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)', merged_at)):
                raise ValueError('merged record lacks a closed state or valid merge time')
            merged_time = datetime.fromisoformat(merged_at.replace('Z', '+00:00'))
            if merged_time > observed_at or merged_time > response_date + timedelta(seconds=1):
                raise ValueError('merge time is in the future of the observation')
            sha = value['merge_commit_sha']
            if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha):
                raise Refusal('evidence_invalid', 'Merged PR lacks an exact provider merge commit.')
            # No head repository or Git ancestry is needed: deleted forks and
            # rewritten squash/rebase commits are proven by this provider record.
            return 'integrated', {'targetCommit': sha, 'pullRequest': copy.deepcopy(pr),
                                  'mergedAt': merged_at, 'mergedHead': value['head']['sha'],
                                  'mergeCommit': sha, 'providerRecordId': value['node_id']}
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
            raise Refusal('evidence_unavailable', 'GitHub returned an incomplete, stale or malformed authoritative record.', True) from error

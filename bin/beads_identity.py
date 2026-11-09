"""Canonical local identities and normal Beads reads/claims; no closure API."""
import json
import os
from pathlib import Path
import subprocess
import uuid

from beads_contract import Refusal, now, validate


def command(argv, cwd, *, env=None, timeout=3):
    clean = dict(os.environ if env is None else env)
    for key in tuple(clean):
        if key.startswith('GIT_'):
            clean.pop(key)
    try:
        result = subprocess.run(argv, cwd=cwd, env=clean, text=True,
                                capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refusal('evidence_unavailable', f'{argv[0]} unavailable: {error}', True) from error
    if result.returncode:
        raise Refusal('evidence_unavailable', f'{argv[0]}: {result.stderr.strip()[:500]}', True)
    return result.stdout.strip()


def machine_id():
    path = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'go-bd/machine-id'
    try:
        value = path.read_text().strip()
    except OSError as error:
        raise Refusal('unknown_machine', f'Existing machine identity unavailable: {path}; restore it, do not replace it.') from error
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError('noncanonical machine identity')
    except (ValueError, AttributeError) as error:
        raise Refusal('unknown_machine', f'Invalid machine identity: {path}; restore the original file.') from error
    return value


def canonical_path(value, code):
    path = Path(value)
    if not path.is_absolute() or str(path.resolve()) != value:
        raise Refusal(code, f'Use the canonical absolute path, not an alias: {value}')
    return path


def repository_identity(root):
    root = str(Path(root).resolve())
    common = command(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], root)
    top = command(['git', 'rev-parse', '--show-toplevel'], root)
    common = str(Path(common).resolve())
    canonical_root = str(Path(common).parent) if Path(common).name == '.git' else str(Path(top).resolve())
    return {'root': canonical_root, 'commonDir': common}


def validate_workspace(workspace, scope):
    validate('run', workspace, 'Workspace')
    path = canonical_path(workspace['path'], 'workspace_mismatch')
    if not path.is_dir():
        raise Refusal('workspace_missing', f'Recorded workspace missing: {path}; no recreation authorized.')
    if workspace['sourceRepository'] != scope['repository']:
        raise Refusal('workspace_mismatch', 'Workspace source repository differs from this run.')
    common = str(Path(command(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], path)).resolve())
    branch = command(['git', 'symbolic-ref', '-q', 'HEAD'], path)
    if common != workspace['commonDir'] or branch != workspace['branchRef']:
        raise Refusal('workspace_mismatch', 'Workspace common directory or branch changed; retain for operator review.')
    if workspace['backing'] == 'linked' and common != scope['repository']['commonDir']:
        raise Refusal('workspace_mismatch', 'Linked workspace does not share the canonical repository.')
    if workspace['backing'] == 'clone' and common == scope['repository']['commonDir']:
        raise Refusal('workspace_mismatch', 'Clone backing conflicts with linked repository identity.')
    return workspace


class Authority:
    def bd(self, scope, *args):
        env = os.environ.copy()
        env['BEADS_DIR'] = scope['beadsDir']
        text = command(['bd', '--sandbox', *args, '--json'], scope['repository']['root'], env=env, timeout=20)
        try:
            return json.loads(text)
        except ValueError as error:
            raise Refusal('invalid_record', 'Beads returned malformed JSON; no fallback database used.') from error

    def validate_scope(self, scope, machine):
        validate('run', scope, 'Scope')
        if scope['machineId'] != machine:
            raise Refusal('remote_machine', 'This controller operates on its recorded local machine only.')
        for path in scope['repository'].values():
            canonical_path(path, 'repository_mismatch')
        canonical_path(scope['beadsDir'], 'database_mismatch')
        if repository_identity(scope['repository']['root']) != scope['repository']:
            raise Refusal('repository_mismatch', 'Request must name the canonical checkout and Git common directory.')
        # Resolve independently of caller-supplied BEADS_DIR, then validate bd's context.
        env = os.environ.copy()
        env.pop('BEADS_DIR', None)
        root = scope['repository']['root']
        if (Path(root) / '.beads').is_dir():
            env['BEADS_DIR'] = str(Path(root) / '.beads')
        location = json.loads(command(['bd', '--sandbox', 'where', '--json'], root, env=env, timeout=20))
        if str(Path(location['path']).resolve()) != scope['beadsDir']:
            raise Refusal('database_mismatch', 'Requested database is not the canonical repository database.')
        context = self.bd(scope, 'context')
        if str(Path(context['repo_root']).resolve()) != root or str(Path(context['beads_dir']).resolve()) != scope['beadsDir']:
            raise Refusal('database_mismatch', 'Beads context does not belong to the canonical repository/database.')

    def owner(self, scope):
        if os.environ.get('BEADS_ACTOR'):
            return os.environ['BEADS_ACTOR']
        try:
            return command(['git', 'config', 'user.name'], scope['repository']['root']) or os.environ['USER']
        except Refusal:
            return os.environ['USER']

    def issue(self, scope):
        rows = self.bd(scope, 'show', scope['issueId'])
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get('id') != scope['issueId']:
            raise Refusal('invalid_request', 'A full, exact issue ID is required; prefixes are not accepted.')
        return rows[0]

    def eligible(self, scope, owner):
        issue = self.issue(scope)
        if issue.get('issue_type') == 'epic':
            raise Refusal('issue_not_ready', 'Epics are sessionless containers; choose a concrete child.')
        if issue.get('assignee') not in (None, '', owner):
            raise Refusal('owner_mismatch', 'Bead is assigned to another owner.')
        if issue.get('status') == 'in_progress':
            if issue.get('assignee') != owner:
                raise Refusal('owner_mismatch', 'An in-progress issue must already belong to the requesting owner.')
            if any(row['id'] == scope['issueId'] for row in self.bd(scope, 'blocked')):
                raise Refusal('issue_blocked', 'Inherited or direct blockers prevent writable execution.')
        elif not any(row['id'] == scope['issueId'] for row in self.bd(scope, 'ready', '--limit', '0')):
            raise Refusal('issue_not_ready', 'Issue is not ready; no claim or launch performed.')
        return issue

    def perform(self, step, run, intent, private):
        if step != 'claim':
            raise Refusal('unsupported_capability', f'Beads cannot perform {step}.')
        self.eligible(run['scope'], run['owner'])
        self.bd(run['scope'], 'update', run['scope']['issueId'], '--claim', '--actor', run['owner'])
        return self.recover(step, run, intent, private)

    def recover(self, step, run, intent, private):
        issue = self.issue(run['scope'])
        if issue.get('status') != 'in_progress' or issue.get('assignee') != run['owner']:
            raise Refusal('issue_changed', 'Claim outcome uncertain or owner changed; inspect Beads before retrying.')
        return {'status': issue['status'], 'assignee': issue['assignee'], 'observedAt': now()}

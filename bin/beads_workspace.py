"""Explicit, retained omp workspaces; no network or integration operations.

The installed omp 18.17 worktree CLI honors worktree.clone/isolation.backend,
but both its copy-on-write and plain checkout paths register *linked Git*
worktrees. Backing describes Git identity, not the filesystem copy technique.
Independent legacy clones require an omp registry binding to the canonical
source; their origin is never evidence of source or integration authority.

Planning queries supported `omp config get` and `worktree list` APIs. These
may initialize omp's own settings storage, but never provision workspaces,
create branches, alter the registry, or claim issues. Provisioning is confined
to perform; recovery only observes the exact journaled resource.

Legacy adoption observes an explicit canonical checkout and its omp/Git
registrations only. It preserves the checkout's actual branch and does not
choose an integration destination or provision any resource.
"""
import copy
import json
import os
from pathlib import Path
import sys

from beads_contract import Refusal, validate
from beads_identity import canonical_path, command, repository_identity, validate_workspace


class OmpWorkspace:
    def __init__(self, *, env=None, executables=None):
        self.env = dict(os.environ if env is None else env)
        self.executables = dict(executables) if executables is not None else None

    def _command(self, argv, run, *, timeout=20, cwd=None):
        env = {**self.env, 'BEADS_DIR': run['scope']['beadsDir']}
        if self.executables is not None:
            executable = self.executables.get(argv[0])
            if not executable:
                raise Refusal('unsupported_capability', f'Configured workspace command is unavailable: {argv[0]}')
            argv = [executable, *argv[1:]]
        return command([str(arg) for arg in argv], cwd or run['scope']['repository']['root'],
                       env=env, timeout=timeout)

    def _json(self, argv, run):
        try:
            return json.loads(self._command(argv, run))
        except ValueError as error:
            raise Refusal('invalid_record', 'omp returned malformed workspace/config JSON.') from error

    def _registry(self, run):
        rows = self._json(['omp', 'worktree', 'list', '--json'], run)
        if not isinstance(rows, list) or any(not isinstance(row, dict) or
                not isinstance(row.get('path'), str) or not Path(row['path']).is_absolute() or
                (row.get('parentRepo') is not None and (not isinstance(row['parentRepo'], str) or
                 not Path(row['parentRepo']).is_absolute())) for row in rows):
            raise Refusal('invalid_record', 'omp worktree list must return workspace records with absolute paths.')
        return rows

    def _absolute_dir(self, value):
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip()
        if value == '~' or value.startswith(('~/', '~\\')):
            value = str(Path(self.env.get('HOME', str(Path.home())))) + value[1:]
        path = Path(value)
        return path.resolve() if path.is_absolute() else None

    def _base(self, run):
        setting = self._json(['omp', 'config', 'get', 'worktree.base', '--json'], run)
        if not isinstance(setting, dict) or setting.get('key') != 'worktree.base' or (
                setting.get('value') is not None and not isinstance(setting['value'], str)):
            raise Refusal('invalid_record', 'omp did not return the worktree.base setting.')
        override = self._absolute_dir(self.env.get('OMP_WORKTREE_DIR')) or self._absolute_dir(setting.get('value'))
        if override:
            return override
        # Mirror pi-utils getWorktreesDir's profile/XDG fallback. Relative
        # environment/config overrides are ignored by omp, not bound to cwd.
        home = Path(self.env.get('HOME', str(Path.home())))
        root = home / (self.env.get('PI_CONFIG_DIR') or '.omp')
        profile = self.env.get('OMP_PROFILE', self.env.get('PI_PROFILE', '')).strip()
        if profile == 'default':
            profile = ''
        if profile:
            root = root / 'profiles' / profile
        agent = self.env.get('PI_CODING_AGENT_DIR') if not profile else None
        default_agent = root / 'agent'
        if sys.platform in ('linux', 'darwin') and (not agent or Path(agent).resolve() == default_agent.resolve()):
            data = self.env.get('XDG_DATA_HOME')
            if data:
                candidate = Path(data) / 'omp'
                if profile:
                    candidate = candidate / 'profiles' / profile
                if candidate.exists():
                    root = candidate
        return (root / 'wt').resolve()

    @staticmethod
    def _name(run):
        issue = run['scope']['issueId']
        if issue in ('.', '..') or '/' in issue or '\\' in issue or issue.startswith('-'):
            raise Refusal('workspace_mismatch', 'Issue ID cannot safely name an omp branch/workspace.')
        attempt = run['fence']['attempt']
        return issue if attempt == 1 else f'{issue}-attempt-{attempt}'

    def _source(self, run, *, require_target=True):
        source = run['scope']['repository']
        for value in source.values():
            canonical_path(value, 'repository_mismatch')
        if repository_identity(source['root']) != source:
            raise Refusal('repository_mismatch', 'Workspace source is not the canonical repository.')
        target = run['target']
        if target is None and not require_target:
            return source
        validate('run', target, 'Target')
        if target['mode'] != 'github-pr' and target['repository'] != source:
            raise Refusal('repository_mismatch', 'Workspace target must name the canonical source, never a clone.')
        self._command(['git', 'check-ref-format', target['ref']], run)
        return source

    def _identity(self, run, path, branch):
        if not path.is_dir():
            raise Refusal('workspace_mismatch', f'Workspace path is not a directory: {path}')
        top = self._command(['git', 'rev-parse', '--show-toplevel'], run, cwd=path)
        if str(Path(top).resolve()) != str(path):
            raise Refusal('workspace_mismatch', 'Workspace path is not the exact checkout root.')
        common = str(Path(self._command(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                                       run, cwd=path)).resolve())
        source = run['scope']['repository']
        return {'provider': 'omp-worktree', 'backing': 'linked' if common == source['commonDir'] else 'clone',
                'path': str(path), 'branchRef': branch, 'sourceRepository': copy.deepcopy(source), 'commonDir': common}

    def _validate(self, run, workspace, rows, *, require_registry=False):
        validate_workspace(workspace, run['scope'])
        path = Path(workspace['path'])
        top = self._command(['git', 'rev-parse', '--show-toplevel'], run, cwd=path)
        if str(Path(top).resolve()) != workspace['path']:
            raise Refusal('workspace_mismatch', 'Recorded workspace is not the exact checkout root.')
        matches = [row for row in rows if str(Path(row['path']).resolve()) == workspace['path']]
        if len(matches) > 1:
            raise Refusal('workspace_mismatch', 'Multiple omp registry entries bind this workspace path.')
        if require_registry and not matches:
            raise Refusal('workspace_mismatch', 'Adoption requires one exact omp source registry binding.')
        branch = workspace['branchRef'].removeprefix('refs/heads/')
        source = run['scope']['repository']
        for row in matches:
            if (row.get('orphanReason') is not None or row.get('kind') in ('orphan', 'orphaned') or row.get('branch') != branch or
                    not row.get('parentRepo') or str(Path(row['parentRepo']).resolve()) != source['root']):
                raise Refusal('workspace_mismatch', 'omp registry no longer binds this path/branch to its canonical source.')
        if workspace['backing'] == 'clone':
            if len(matches) != 1:
                raise Refusal('workspace_mismatch', 'Independent clone requires one exact omp source registry binding; origin is not authority.')
        else:
            listing = self._command(['git', 'worktree', 'list', '--porcelain', '-z'], run)
            records = [dict(field.split(' ', 1) for field in record.split('\0') if ' ' in field)
                       for record in listing.split('\0\0') if record]
            if not any(row.get('worktree') and str(Path(row['worktree']).resolve()) == workspace['path'] and
                       row.get('branch') == workspace['branchRef'] for row in records):
                raise Refusal('workspace_mismatch', 'Linked workspace is absent from the canonical Git worktree registry.')
        return workspace

    def adopt(self, run, path):
        self._source(run, require_target=False)
        if not isinstance(path, str) or not path:
            raise Refusal('workspace_mismatch', 'Adoption requires the exact recorded workspace path.')
        destination = canonical_path(path, 'workspace_mismatch')
        if not destination.is_dir():
            raise Refusal('workspace_missing', 'Recorded legacy workspace is missing; no recreation authorized.')
        branch = self._command(['git', 'symbolic-ref', '-q', 'HEAD'], run, cwd=destination)
        workspace = self._identity(run, destination, branch)
        return copy.deepcopy(self._validate(run, workspace, self._registry(run), require_registry=True))

    def plan(self, run):
        source = self._source(run)
        name = self._name(run)
        branch = f'refs/heads/{name}'
        self._command(['git', 'check-ref-format', branch], run)
        base = self._base(run)
        destination = base / name
        rows = self._registry(run)
        matches = [row for row in rows if row.get('branch') == name and row.get('parentRepo') and
                   str(Path(row['parentRepo']).resolve()) == source['root'] and Path(row['path']).is_dir()]
        if len(matches) > 1:
            raise Refusal('workspace_mismatch', 'Multiple omp workspaces bind the same issue branch; select resources manually.')
        if not destination.exists() and not destination.is_symlink() and matches:
            destination = Path(matches[0]['path']).resolve()
        if destination.is_symlink() and not destination.exists():
            raise Refusal('workspace_mismatch', 'Reserved workspace path is a dangling symlink; retain it for operator review.')
        destination = destination.resolve()
        if destination.exists() or destination.is_symlink():
            workspace = self._identity(run, destination, branch)
            return copy.deepcopy(self._validate(run, workspace, rows))
        # Check refs locally only. No remote is needed for a local-target run;
        # absent target history must be supplied by the operator, never fetched.
        self._command(['git', 'rev-parse', '--verify', f"{run['target']['ref']}^{{commit}}"], run)
        refs = self._command(['git', 'for-each-ref', '--format=%(refname)', branch], run).splitlines()
        if branch in refs:
            raise Refusal('workspace_mismatch', 'Issue branch exists without its exact registered workspace; no implicit recreation authorized.')
        workspace = {'provider': 'omp-worktree', 'backing': 'linked', 'path': str(destination), 'branchRef': branch,
                     'sourceRepository': copy.deepcopy(source), 'commonDir': source['commonDir']}
        validate('run', workspace, 'Workspace')
        return workspace

    def _planned(self, run, private):
        workspace = private.get('plannedWorkspace')
        if not isinstance(workspace, dict):
            raise Refusal('invalid_record', 'Workspace allocation requires its durable resource plan.')
        validate('run', workspace, 'Workspace')
        self._source(run)
        canonical_path(workspace['path'], 'workspace_mismatch')
        canonical_path(workspace['commonDir'], 'workspace_mismatch')
        if workspace['sourceRepository'] != run['scope']['repository'] or workspace['branchRef'] != f'refs/heads/{self._name(run)}':
            raise Refusal('workspace_mismatch', 'Planned workspace differs from this immutable run scope/attempt.')
        return workspace

    def perform(self, step, run, intent, private):
        if step != 'workspace':
            raise Refusal('unsupported_capability', f'Workspace provider cannot perform {step}.')
        workspace = self._planned(run, private)
        if self.plan(run) != workspace:
            raise Refusal('workspace_mismatch', 'Workspace settings/resources changed after planning; no allocation performed.')
        path = Path(workspace['path'])
        if not path.exists():
            self._command(['omp', 'worktree', 'add', '-C', run['scope']['repository']['root'],
                           '-b', workspace['branchRef'].removeprefix('refs/heads/'),
                           path, run['target']['ref']], run, timeout=120)
        return self.recover(step, run, intent, private)

    def recover(self, step, run, intent, private):
        if step != 'workspace':
            raise Refusal('unsupported_capability', f'Workspace provider cannot recover {step}.')
        workspace = self._planned(run, private)
        path = Path(workspace['path'])
        if not path.exists() or (path.is_dir() and not (path / '.git').exists()):
            return None
        self._validate(run, workspace, self._registry(run))
        return {'workspace': copy.deepcopy(workspace)}

    def validate(self, run):
        self._source(run, require_target=False)
        workspace = run.get('workspace')
        if not workspace:
            raise Refusal('workspace_missing', 'No acknowledged omp workspace exists.')
        return self._validate(run, workspace, self._registry(run), require_registry=run['target'] is None)

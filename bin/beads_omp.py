"""Concrete omp protocol: exact transcripts, leases, foreground and startup receipts.

Herdr only hosts terminals; OmpWorkspace independently provisions workspaces.
Operation files are durable before terminal creation. Recovery observes the exact
creation argv/ack and never issues a second terminal request.
"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from beads_contract import Refusal, now, validate
from beads_herdr import HerdrHosting
from beads_identity import Authority, command


def durable_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.operation-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise Refusal('invalid_record', f'Cannot read retained omp operation {path}: {error}') from error


def live_omp(info):
    if info.get('state') != 'live':
        return False
    foreground = info.get('foreground') or {}
    argv = foreground.get('argv') or info.get('argv') or []
    name = Path(foreground.get('name') or (argv[0] if argv else '')).name
    return name == 'omp' or any(Path(str(arg)).name == 'omp' for arg in argv[:2])


class OmpExecution:
    capabilities = ['launch', 'exact_resume', 'durable_identity', 'owner_activity', 'structured_handoff']

    def __init__(self, authority=None, hosting=None, state=None, socket_path=None, executables=None):
        self.executables = dict(executables) if executables is not None else None
        self.authority = authority if authority is not None else Authority()
        self.hosting = hosting if hosting is not None else HerdrHosting(executables=executables)
        self.state = Path(state if state is not None else
                          Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'herdr-beads/omp').resolve()
        self.extension = Path(__file__).resolve().with_name('go-bd-session.mjs')
        if socket_path is None:
            from beads_transport import default_socket_path
            socket_path = default_socket_path()
        self.socket_path = str(socket_path)

    def binary(self, name):
        value = self.executables.get(name) if self.executables is not None else shutil.which(name)
        if not value:
            raise Refusal('unsupported_capability', f'Required local command is not installed: {name}')
        return value

    def path(self, run):
        # UUID assigned by the controller, not an issue/title/session search.
        fence = run['fence']
        if str(uuid.UUID(fence['runId'])) != fence['runId']:
            raise Refusal('invalid_record', 'Invalid run identity for omp operation storage.')
        return self.state / fence['runId'] / f"{fence['generation']}.json"

    def record(self, run, *, required=False):
        issue = self.authority.issue(run['scope'])
        metadata = issue.get('metadata') or {}
        if not isinstance(metadata, dict):
            raise Refusal('invalid_record', 'Bead metadata must be an object; retained intact.')
        if 'omp_session' not in metadata:
            if required:
                raise Refusal('session_missing', 'Exact recorded omp association missing; no fresh replacement is safe.')
            return None
        return self.validate_record(run, metadata['omp_session'])

    def validate_record(self, run, record):
        validate('adapter', record, 'LegacyOmpSession')
        if record['machine'] != run['scope']['machineId']:
            raise Refusal('remote_machine', 'Saved omp session belongs to another machine; history retained.')
        transcript, worktree = Path(record['session_file']), Path(record['worktree'])
        if str(transcript.resolve()) != str(transcript) or str(worktree.resolve()) != str(worktree):
            raise Refusal('session_mismatch', 'Saved transcript/workspace paths must be canonical.')
        if not transcript.is_file():
            raise Refusal('session_missing', f'Saved transcript missing: {transcript}; no fresh session.')
        if not worktree.is_dir():
            raise Refusal('workspace_missing', f'Saved workspace missing: {worktree}; no recreation.')
        try:
            with transcript.open() as handle:
                lines = handle.read(65536).splitlines()[:3]
            entries = [json.loads(line) for line in lines if line.strip()]
        except (OSError, ValueError) as error:
            raise Refusal('session_mismatch', f'Cannot read exact omp transcript: {error}') from error
        header = next((entry for entry in entries if isinstance(entry, dict) and entry.get('type') == 'session'), {})
        if header.get('id') != record['session_id'] or not isinstance(header.get('cwd'), str) or str(Path(header['cwd']).resolve()) != record['worktree']:
            raise Refusal('session_mismatch', 'Transcript ID/directory differs from the exact Beads association.')
        workspace = run.get('workspace')
        if workspace and workspace['path'] != record['worktree']:
            raise Refusal('workspace_mismatch', 'Recorded session belongs to another workspace.')
        session = (run.get('execution') or {}).get('session')
        if session and session['sessionId'] != record['session_id']:
            raise Refusal('session_mismatch', 'Beads association changed outside the retained run.')
        return record

    def lease(self, record):
        try:
            result = json.loads(command([self.binary('omp'), 'session', 'lease', '--json', '--', record['session_id']],
                                        record['worktree'], timeout=20))
        except ValueError as error:
            raise Refusal('session_owned', 'Cannot establish exact omp lease ownership.') from error
        if (not isinstance(result, dict) or not isinstance(result.get('held'), list)
                or not isinstance(result.get('unknown'), list)):
            raise Refusal('session_owned', 'Invalid omp lease response; resume refused.')
        if record['session_id'] in result['unknown']:
            return 'unknown'
        return 'held' if record['session_id'] in result['held'] else 'free'

    def preflight(self, run):
        for name in ('omp', 'bd', 'hc', 'herdr-app'):
            self.binary(name)
        if not self.extension.is_file():
            raise Refusal('unsupported_capability', f'Required omp extension missing: {self.extension}')
        self.hosting.preflight()
        session = (run.get('execution') or {}).get('session')
        record = self.record(run, required=bool(session))
        if record and not session:
            raise Refusal('session_owned', 'Existing omp history requires explicit legacy adoption; never replace it with a new run.')
        if session:
            return record
        return None

    def inspect(self, run, private):
        record = self.record(run, required=True)
        lease = self.lease(record)
        terminal = run['execution']['terminal']
        if terminal:
            info = self.hosting.lookup(terminal['address'])
            if info is not None:
                self.hosting.validate(run, info)
            if info is not None and live_omp(info):
                config = read_json(Path(private.get('adapter', {}).get('configPath', self.path(run))))
                if not config:
                    return 'unknown'
                ack = self.ack(run, config, info)
                if ack and lease == 'held':
                    return 'active'
                return 'unknown'
        if lease != 'free':
            raise Refusal('session_owned', 'Exact transcript is leased or ownership is unknown; no replacement terminal.')
        return 'inactive'

    def config(self, run, intent, private):
        previous = self.record(run, required=intent['steps'][intent['index']] == 'resume')
        if previous and self.lease(previous) != 'free':
            raise Refusal('session_owned', 'Exact transcript is already leased; resume refused.')
        path = self.path(run)
        config = {'operationId': intent['operationId'], 'issue': run['scope']['issueId'],
                  'root': run['scope']['repository']['root'], 'beads': run['scope']['beadsDir'],
                  'machine': run['scope']['machineId'], 'worktree': run['workspace']['path'],
                  'previous': previous, 'bd': self.binary('bd'), 'ack': str(path.with_suffix('.ack.json')),
                  'schemaVersion': 1, 'scope': run['scope'], 'fence': run['fence'],
                  'bindingId': private['bindingId'], 'credential': private['credential'],
                  'controllerSocket': self.socket_path, 'terminal': None,
                  'access': run['execution']['access']}
        if not previous:
            issue = self.authority.issue(run['scope'])
            task = (f"Bead {run['scope']['issueId']} is claimed and in progress. Start working on it.\n"
                    f"Use the shared Beads database at {run['scope']['beadsDir']} (BEADS_DIR is set).\n"
                    'Agents do not close Beads, delete workspaces, merge, or push. The trusted controller '
                    'records lifecycle transitions only after independent verification. Closure and cleanup '
                    'remain separate owner-reviewed manual actions; handoff, idle, and release are not merge evidence.\n\n'
                    + '\n'.join(f'{key}: {value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}'
                                for key, value in issue.items()))
            prompt = path.with_suffix('.prompt.txt')
            config['prompt_file'] = str(prompt)
            prompt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(prompt, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as handle:
                handle.write(task)
                handle.flush()
                os.fsync(handle.fileno())
        durable_json(path, config)
        return config

    def ack(self, run, config, info):
        ack = read_json(Path(config['ack']))
        if ack is None:
            return None
        if not isinstance(ack, dict):
            raise Refusal('session_mismatch', 'Invalid omp startup acknowledgement.')
        if ack.get('error'):
            raise Refusal('session_mismatch', str(ack['error']))
        record = self.record(run, required=True)
        if ack.get('session') != record:
            raise Refusal('session_mismatch', 'Startup receipt does not match the durable exact Beads association.')
        if config['previous'] and record != config['previous']:
            raise Refusal('session_mismatch', 'Resume changed the recorded transcript identity; history retained.')
        if record['worktree'] != config['worktree'] or record['machine'] != config['machine']:
            raise Refusal('session_mismatch', 'Startup receipt belongs to another machine/workspace.')
        foreground = info.get('foreground') or {}
        if type(ack.get('pid')) is not int or foreground.get('pid') != ack['pid']:
            return None
        if not live_omp(info) or (info.get('status') or {}).get('app') != 'omp':
            return None
        if self.lease(record) != 'held':
            return None
        return ack

    def perform(self, step, run, intent, private):
        if step == 'focus':
            if self.inspect(run, private) != 'active':
                raise Refusal('session_owned', 'Exact prior process is not active; refresh before resume.')
            info = self.hosting.info(run['execution']['terminal']['address'])
            self.hosting.focus(run, info)
            if self.inspect(run, private) != 'active':
                raise Refusal('session_owned', 'omp exited while focusing; no replacement launched.')
            return {'focused': True}
        if step not in ('launch', 'resume') or run['execution']['access'] != 'writable':
            raise Refusal('unsupported_capability', f'Installed omp adapter cannot enforce {step}/{run["execution"]["access"]}.')
        self.preflight(run)
        config = self.config(run, intent, private)
        argv = [self.binary('omp'), '--extension', str(self.extension)]
        if config['previous']:
            argv += ['--resume', config['previous']['session_file']]
        # No startup prompt on argv: extension delivers it only after durability.
        self.hosting.start(run, ['env', f'BEADS_DIR={run["scope"]["beadsDir"]}',
                                 f'GO_BD_SESSION_CONFIG={self.path(run)}', *argv])
        return self.recover(step, run, intent, private)

    def recover(self, step, run, intent, private):
        if step == 'focus':
            return {'focused': True} if self.inspect(run, private) == 'active' else None
        if step not in ('launch', 'resume'):
            raise Refusal('unsupported_capability', f'No omp recovery protocol for {step}.')
        config = read_json(self.path(run))
        if config is None:
            return None
        if (config.get('operationId') != intent['operationId'] or config.get('fence') != run['fence']
                or config.get('bindingId') != private['bindingId']):
            raise Refusal('session_mismatch', 'Retained startup intent belongs to another operation/incarnation.')
        info = self.hosting.find_operation(run, self.path(run))
        if info is None:
            return None
        self.hosting.associate(run, info['address'])
        info = self.hosting.info(info['address'])
        self.hosting.validate(run, info)
        ack = self.ack(run, config, info)
        if ack is None:
            return None
        observation = {'fence': run['fence'], 'bindingId': private['bindingId'],
                       'eventId': intent['operationId'] + '-bound', 'sequence': private['sequence'] + 1,
                       'observedAt': now(), 'event': {'kind': 'session_bound',
                       'session': {'harness': 'omp', 'sessionId': ack['session']['session_id'], 'bindingId': private['bindingId']},
                       'terminal': self.hosting.terminal(run, info), 'activity': 'active'}}
        binding = read_json(Path(str(self.path(run)) + '.binding.json'))
        if binding is not None:
            self.validate_binding(run, binding)
            observation = ack.get('observation') or binding.get('startupObservation') or binding['observation']
            if observation['event']['kind'] != 'session_bound':
                raise Refusal('session_mismatch', 'Startup binding no longer contains its session-bound observation.')
        return {'observation': observation, 'private': {'configPath': str(self.path(run))}}

    def validate_binding(self, run, binding):
        if not isinstance(binding, dict):
            raise Refusal('session_mismatch', 'Malformed durable omp binding.')
        identity = {key: binding.get(key) for key in
                    ('schemaVersion', 'fence', 'binding', 'machineId', 'workspacePath', 'omp')}
        validate('adapter', identity, 'OmpBinding')
        if binding['fence'] != run['fence'] or binding['machineId'] != run['scope']['machineId'] or binding['workspacePath'] != run['workspace']['path']:
            raise Refusal('session_mismatch', 'Durable binding belongs to another incarnation/machine/workspace.')
        self.validate_record(run, binding['omp'])

    def validate_observation(self, run, observation, private):
        event = observation['event']
        if event['kind'] not in ('session_bound', 'activity', 'exited'):
            raise Refusal('unsupported_capability', 'Installed omp adapter does not support this lifecycle observation.')
        config = read_json(self.path(run))
        if not config or config.get('bindingId') != private['bindingId'] or config.get('fence') != run['fence']:
            raise Refusal('session_mismatch', 'No exact durable startup configuration for this binding.')
        terminal = event.get('terminal') if event['kind'] == 'session_bound' else run['execution']['terminal']
        if not terminal or terminal['machineId'] != run['scope']['machineId']:
            raise Refusal('terminal_mismatch', 'Session acknowledgement needs the exact local terminal.')
        info = self.hosting.find_operation(run, self.path(run), address=terminal['address'])
        if not info or info['address'] != terminal['address']:
            raise Refusal('terminal_mismatch', 'Terminal does not carry this operation startup identity.')
        self.hosting.validate(run, info)
        binding = read_json(Path(str(self.path(run)) + '.binding.json'))
        if binding is not None:
            self.validate_binding(run, binding)
            session = event['session'] if event['kind'] == 'session_bound' else run['execution']['session']
            if (binding.get('observation') != observation or binding['binding'] != session
                    or binding['binding']['bindingId'] != private['bindingId']
                    or binding.get('pid') != (info.get('foreground') or {}).get('pid')
                    or not live_omp(info) or self.lease(binding['omp']) != 'held'):
                raise Refusal('session_mismatch', 'Observation is not backed by the exact durable omp binding/process.')
        else:
            if event['kind'] != 'session_bound':
                raise Refusal('session_mismatch', 'Activity requires the exact durable extension observation.')
            ack = self.ack(run, config, info)
            if not ack or event['session'] != {'harness': 'omp', 'sessionId': ack['session']['session_id'], 'bindingId': private['bindingId']}:
                raise Refusal('session_mismatch', 'Observation is not backed by the exact durable omp process/receipt.')

    def publish_observation(self, run, observation, private):
        if observation['event']['kind'] != 'session_bound':
            return
        config = read_json(self.path(run))
        binding = read_json(Path(str(self.path(run)) + '.binding.json'))
        # The current startup extension has already mirrored its association.
        # Controller-protocol extensions publish a validated private binding.
        if binding is None:
            self.record(run, required=True)
            return
        self.validate_binding(run, binding)
        record = binding['omp']
        issue = self.authority.issue(run['scope'])
        if issue.get('status') != 'in_progress' or issue.get('assignee') != run['owner']:
            raise Refusal('owner_mismatch', 'Bead owner/status changed before association publication.')
        metadata = issue.get('metadata') or {}
        if not isinstance(metadata, dict):
            raise Refusal('invalid_record', 'Cannot replace malformed Beads metadata.')
        current = metadata.get('omp_session')
        if current not in (config['previous'], record):
            raise Refusal('session_mismatch', 'Beads association changed outside this operation; history retained.')
        if current != record:
            self.authority.bd(run['scope'], 'update', run['scope']['issueId'],
                              '--metadata', json.dumps({'omp_session': record}))
        if self.record(run, required=True) != record:
            raise Refusal('session_mismatch', 'Canonical Beads association was not durably published.')


def create_adapters(executables):
    """Static service composition; null tools never fall back to ambient PATH."""
    from beads_workspace import OmpWorkspace
    for name, path in executables.items():
        if path is not None and (not isinstance(path, str) or not Path(path).is_absolute()):
            raise Refusal('unsupported_capability', f'Service executable {name} must be an absolute path or null.')
    execution = OmpExecution(executables=executables)
    if any(not executables.get(name) for name in ('omp', 'bd', 'hc', 'herdr-app')):
        execution.capabilities = []
    return execution, OmpWorkspace(executables=executables)

"""Read-only legacy go-bd evidence; never discover a session by recency or title."""
import copy
import fcntl
import hashlib
import os
from pathlib import Path
import uuid

from beads_contract import Refusal


def canonical(value, expected, label):
    if (not isinstance(value, str) or not Path(value).is_absolute()
            or str(Path(value).resolve()) != value or value != expected):
        raise Refusal('session_mismatch', f'Legacy {label} differs from the canonical adoption context.')


def launch_path(path, directory):
    if path.parent != directory or not path.name.startswith('launch-') or path.suffix != '.json':
        raise Refusal('invalid_record', 'Legacy startup configuration is outside its canonical project state.')
    try:
        identifier = path.stem.removeprefix('launch-')
        if str(uuid.UUID(identifier)) != identifier:
            raise ValueError('noncanonical UUID')
    except ValueError as error:
        raise Refusal('invalid_record', 'Legacy startup configuration has an invalid launch identity.') from error
    canonical(str(path), str(path.resolve()), 'configuration path')


def validate_launch(adapter, run, record, evidence, directory):
    path = Path(evidence['configPath'])
    launch_path(path, directory)
    config, ack = evidence['config'], evidence['ack']
    if not isinstance(config, dict) or config.get('issue') != run['scope']['issueId']:
        raise Refusal('session_mismatch', 'Legacy startup configuration names another issue.')
    for key, expected in (('root', run['scope']['repository']['root']),
                          ('beads', run['scope']['beadsDir']), ('worktree', record['worktree'])):
        canonical(config.get(key), expected, key)
    if config.get('machine') != run['scope']['machineId']:
        raise Refusal('remote_machine', 'Legacy startup configuration belongs to another machine.')
    if 'previous' not in config or config['previous'] not in (None, record):
        raise Refusal('session_mismatch', 'Legacy startup configuration carries a different exact session.')
    ack_path = path.with_name(path.stem + '-ack.json')
    canonical(config.get('ack'), str(ack_path), 'acknowledgement path')
    if (not isinstance(ack, dict) or ack.get('error') or ack.get('session') != record
            or type(ack.get('pid')) is not int or ack['pid'] <= 0):
        raise Refusal('session_mismatch', 'Legacy startup acknowledgement is missing, malformed, or conflicts with the exact session.')
    adapter.validate_record(run, ack['session'])


def validate_receipt(receipt, run, record):
    if receipt is None:
        return
    if (not isinstance(receipt, dict) or any(not isinstance(receipt.get(key), str) or not receipt[key]
                                            for key in ('terminal', 'space', 'tab'))):
        raise Refusal('invalid_record', 'Legacy terminal receipt is malformed; retained intact.')
    if '/' in receipt['terminal']:
        raise Refusal('remote_machine', 'Legacy receipt names a remote terminal.')
    for key, expected in (('issue', run['scope']['issueId']), ('project', run['scope']['repository']['root']),
                          ('beads', run['scope']['beadsDir']), ('worktree', record['worktree']),
                          ('machine', run['scope']['machineId']), ('session_id', record['session_id'])):
        if key in receipt and receipt[key] != expected:
            raise Refusal('session_mismatch', f'Legacy terminal receipt conflicts with its exact {key}.')


def observe(adapter, run, evidence):
    """Reconcile current hosting/lease against retained evidence, without legacy reads."""
    record = adapter.record(run, required=True)
    if record != evidence['record']:
        raise Refusal('session_mismatch', 'Exact Beads session changed since legacy adoption.')
    directory = Path(evidence['directory'])
    # Retained evidence is self-contained: state-home changes after adoption do
    # not invalidate it, but every retained launch must share its original root.
    if not directory.is_absolute() or str(directory.resolve()) != str(directory):
        raise Refusal('invalid_record', 'Retained legacy evidence directory is not canonical.')
    launches = {}
    for launch in evidence['launches']:
        validate_launch(adapter, run, record, launch, directory)
        if launch['configPath'] in launches:
            raise Refusal('invalid_record', 'Duplicate retained legacy launch identity.')
        launches[launch['configPath']] = launch
    receipt = evidence['receipt']
    validate_receipt(receipt, run, record)
    rows = adapter.hosting.call(['hc', 'ls', '--all', '--json']).get('terminals')
    if not isinstance(rows, list):
        raise Refusal('terminal_mismatch', 'Herdr did not return terminals for legacy adoption.')
    labels_expected = {'beads.project': run['scope']['beadsDir'], 'beads.issue': run['scope']['issueId']}
    active = []
    observed_addresses = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('address'), str):
            raise Refusal('terminal_mismatch', 'Herdr returned an invalid terminal identity.')
        address = row['address']
        if address in observed_addresses:
            raise Refusal('terminal_mismatch', 'Herdr returned duplicate terminal identities.')
        observed_addresses.add(address)
        receipt_match = receipt is not None and address == receipt['terminal']
        if '/' in address:
            row_labels = row.get('labels') or {}
            if not isinstance(row_labels, dict):
                raise Refusal('terminal_mismatch', 'Herdr returned malformed remote terminal labels.')
            if receipt_match or all(row_labels.get(key) == value for key, value in labels_expected.items()):
                raise Refusal('remote_machine', 'Legacy resources include a foreign terminal.')
            continue
        info = adapter.hosting.info(address)
        if not isinstance(info, dict) or info.get('address') != address:
            raise Refusal('terminal_mismatch', 'Herdr terminal identity changed during adoption.')
        labels = info.get('labels') or {}
        argv = info.get('argv') or []
        if not isinstance(labels, dict) or not isinstance(argv, list) or any(not isinstance(arg, str) for arg in argv):
            raise Refusal('terminal_mismatch', 'Herdr returned malformed terminal labels/creation argv.')
        markers = [arg.removeprefix('GO_BD_SESSION_CONFIG=') for arg in argv if arg.startswith('GO_BD_SESSION_CONFIG=')]
        marker_match = any(marker in launches for marker in markers)
        label_match = all(labels.get(key) == value for key, value in labels_expected.items())
        if not (receipt_match or marker_match or label_match):
            continue
        if any(labels.get(key) != value for key, value in labels_expected.items()):
            raise Refusal('terminal_mismatch', 'Legacy terminal receipt/configuration conflicts with its issue labels.')
        if markers:
            databases = [arg.removeprefix('BEADS_DIR=') for arg in argv if arg.startswith('BEADS_DIR=')]
            if databases != [run['scope']['beadsDir']]:
                raise Refusal('session_mismatch', 'Legacy creation argv conflicts with the canonical Beads database.')
        foreground, status = info.get('foreground') or {}, info.get('status') or {}
        if not isinstance(foreground, dict) or not isinstance(status, dict):
            raise Refusal('terminal_mismatch', 'Legacy terminal has malformed foreground/status evidence.')
        process_argv = foreground.get('argv') or []
        if (not isinstance(process_argv, list) or any(not isinstance(arg, str) for arg in process_argv)
                or not isinstance(foreground.get('name', ''), str)):
            raise Refusal('terminal_mismatch', 'Legacy terminal has malformed foreground process identity.')
        cwd = info.get('cwd') or foreground.get('cwd')
        canonical(cwd, record['worktree'], 'terminal workspace')
        terminal_run = dict(run, workspace={'path': record['worktree']})
        terminal = adapter.hosting.terminal(terminal_run, info)
        for key, field in (('herdr.space', 'spaceId'), ('herdr.tab', 'tabId')):
            if key in labels and labels[key] != terminal.get(field):
                raise Refusal('terminal_mismatch', 'Legacy terminal layout labels conflict with its actual location.')
        if receipt_match and (terminal.get('spaceId') != receipt['space'] or terminal.get('tabId') != receipt['tab']):
            raise Refusal('terminal_mismatch', 'Legacy terminal receipt conflicts with its current space/tab.')
        if (receipt_match or markers) and (len(markers) != 1 or markers[0] not in launches):
            raise Refusal('session_mismatch', 'Legacy terminal carries an orphan or conflicting startup configuration.')
        from beads_omp import live_omp
        if not live_omp(info):
            continue
        if receipt is None or not receipt_match:
            raise Refusal('terminal_mismatch', 'Live legacy terminal is not the exact retained receipt terminal.')
        if len(markers) != 1 or markers[0] not in launches:
            raise Refusal('session_mismatch', 'Live labeled terminal lacks its exact original startup configuration.')
        if 'creationArgv' in evidence and argv != evidence['creationArgv']:
            raise Refusal('session_mismatch', 'Adopted terminal creation argv changed outside the retained evidence.')
        launch = launches[markers[0]]
        if (status.get('app') != 'omp' or type(foreground.get('pid')) is not int
                or foreground['pid'] != launch['ack']['pid']):
            raise Refusal('session_mismatch', 'Legacy live process differs from its acknowledged omp PID/session.')
        active.append(terminal)
    if len(active) > 1:
        raise Refusal('terminal_mismatch', 'Multiple live terminals claim the legacy session.')
    lease = adapter.lease(record)
    if active:
        if lease != 'held':
            raise Refusal('session_owned', 'Acknowledged legacy process does not hold the exact session lease.')
        return active[0], 'active'
    if lease != 'free':
        raise Refusal('session_owned', 'Exact legacy transcript is leased or ownership is unknown; no adoption.')
    return None, 'inactive'


def adopt_locked(adapter, run, intent, private, read_json):
    record = adapter.record(run, required=True)
    for value in (run['scope']['beadsDir'], run['scope']['repository']['root'], run['scope']['repository']['commonDir']):
        canonical(value, value, 'scope path')
    directory = (Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'go-bd'
                 / hashlib.sha256(run['scope']['beadsDir'].encode()).hexdigest()).resolve()
    issue_id = run['scope']['issueId']
    if not issue_id or '/' in issue_id or issue_id in ('.', '..'):
        raise Refusal('invalid_record', 'Invalid issue identity for legacy evidence lookup.')
    receipt_path = directory / f'issue-{issue_id}.json'
    canonical(str(receipt_path), str(receipt_path.resolve()), 'receipt path')
    receipt = read_json(receipt_path)
    if receipt is None and receipt_path.exists():
        raise Refusal('invalid_record', 'Legacy terminal receipt is null rather than absent.')
    validate_receipt(receipt, run, record)
    launches = []
    try:
        paths = sorted(directory.glob('launch-*.json'))
        for path in paths:
            if path.name.endswith('-ack.json'):
                ack = read_json(path)
                session = ack.get('session') if isinstance(ack, dict) else None
                if isinstance(session, dict) and session.get('session_id') == record['session_id']:
                    config_path = path.with_name(path.name.removesuffix('-ack.json') + '.json')
                    if not config_path.is_file():
                        raise Refusal('invalid_record', 'Orphan acknowledgement for the exact legacy session; retained for review.')
                continue
            config = read_json(path)
            if not isinstance(config, dict):
                raise Refusal('invalid_record', 'Malformed legacy launch configuration; adoption requires review.')
            if config.get('issue') != issue_id:
                continue
            launch_path(path, directory)
            launch = {'configPath': str(path), 'config': config,
                      'ack': read_json(path.with_name(path.stem + '-ack.json'))}
            validate_launch(adapter, run, record, launch, directory)
            launches.append(launch)
    except OSError as error:
        raise Refusal('invalid_record', f'Cannot inspect legacy startup records: {error}') from error
    evidence = {'fence': copy.deepcopy(run['fence']), 'bindingId': private['bindingId'],
                'record': copy.deepcopy(record), 'directory': str(directory),
                'receiptPath': str(receipt_path), 'receipt': receipt, 'launches': launches}
    terminal, activity = observe(adapter, run, evidence)
    if terminal:
        evidence['creationArgv'] = copy.deepcopy(adapter.hosting.info(terminal['address'])['argv'])
    return {'workspacePath': record['worktree'],
            'session': {'harness': 'omp', 'sessionId': record['session_id'], 'bindingId': private['bindingId']},
            'terminal': terminal, 'activity': activity, 'private': {'legacyAdoption': evidence}}


def adopt(adapter, run, intent, private, read_json):
    issue_id = run['scope']['issueId']
    if not issue_id or '/' in issue_id or issue_id in ('.', '..'):
        raise Refusal('invalid_record', 'Invalid issue identity for legacy evidence lookup.')
    directory = (Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'go-bd'
                 / hashlib.sha256(run['scope']['beadsDir'].encode()).hexdigest()).resolve()
    if not directory.exists():
        return adopt_locked(adapter, run, intent, private, read_json)
    try:
        descriptor = os.open(directory / f'issue-{issue_id}.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Refusal('evidence_unavailable', 'Legacy launcher owns this issue lock; retry after it finishes.', True) from error
            return adopt_locked(adapter, run, intent, private, read_json)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise Refusal('evidence_unavailable', f'Cannot coordinate with the legacy launcher: {error}', True) from error

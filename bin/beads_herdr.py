"""Local terminal presentation only; no harness/session interpretation."""
import json
from pathlib import Path

from beads_contract import Refusal
from beads_identity import command


class HerdrHosting:
    def __init__(self, *, executables=None):
        self.executables = dict(executables) if executables is not None else None

    def call(self, argv):
        if self.executables is not None:
            executable = self.executables.get(argv[0])
            if not executable:
                raise Refusal('unsupported_capability', f'Configured hosting command is unavailable: {argv[0]}')
            argv = [executable, *argv[1:]]
        try:
            value = json.loads(command(argv, None, timeout=20))
        except ValueError as error:
            raise Refusal('terminal_mismatch', 'Herdr returned malformed JSON; retain resources.') from error
        if not isinstance(value, dict) or value.get('error'):
            raise Refusal('evidence_unavailable', f'Herdr command failed: {value}', True)
        return value

    def app(self, command_name, **args):
        return self.call(['herdr-app', 'do', command_name, json.dumps(args)])

    def spaces(self):
        value = self.call(['herdr-app', 'ls'])
        if not isinstance(value.get('spaces'), list):
            raise Refusal('terminal_mismatch', 'Herdr did not return local spaces.')
        return [space for space in value['spaces'] if not space.get('machine')]

    def preflight(self):
        self.spaces()  # Refuse before claim/provisioning when no local window exists.

    def info(self, address):
        if '/' in address:
            raise Refusal('remote_machine', 'Remote terminals cannot host a local run.')
        return self.call(['hc', 'get', address, '--json'])

    def lookup(self, address):
        rows = self.call(['hc', 'ls', '--all', '--json']).get('terminals')
        if not isinstance(rows, list):
            raise Refusal('terminal_mismatch', 'Herdr did not return a terminal list.')
        return self.info(address) if any(row.get('address') == address for row in rows) else None

    @staticmethod
    def labels(run):
        return {'beads.project': run['scope']['beadsDir'], 'beads.issue': run['scope']['issueId']}

    def validate(self, run, info):
        if '/' in info.get('address', '/'):
            raise Refusal('remote_machine', 'Terminal is not on this machine.')
        labels = info.get('labels') or {}
        if any(labels.get(key) != value for key, value in self.labels(run).items()):
            raise Refusal('terminal_mismatch', 'Terminal labels do not bind the canonical Bead.')
        cwd = info.get('cwd') or (info.get('foreground') or {}).get('cwd')
        if not cwd or str(Path(cwd).resolve()) != run['workspace']['path']:
            raise Refusal('terminal_mismatch', 'Terminal belongs to another workspace.')

    def associate(self, run, address):
        self.call(['hc', 'label', address, *[f'{key}={value}' for key, value in self.labels(run).items()],
                   '--json'])

    def find_operation(self, run, config_path, *, address=None):
        # The immutable creation argv carries this exact operation's config path,
        # even if the app reply or subsequent labeling was interrupted.
        rows = ([{'address': address}] if address is not None else
                self.call(['hc', 'ls', '--all', '--json']).get('terminals'))
        if not isinstance(rows, list):
            raise Refusal('terminal_mismatch', 'Herdr returned no terminal list.')
        matches = []
        marker = f'GO_BD_SESSION_CONFIG={config_path}'
        for row in rows:
            address = row.get('address')
            if not address or '/' in address:
                continue
            info = self.info(address)
            if marker not in (info.get('argv') or []):
                continue
            labels = info.get('labels') or {}
            if any(labels.get(key) not in (None, value) for key, value in self.labels(run).items()):
                raise Refusal('terminal_mismatch', 'Operation terminal was associated with another Bead.')
            matches.append(info)
        if len(matches) > 1:
            raise Refusal('terminal_mismatch', 'Multiple terminals carry this operation; retain all for review.')
        return matches[0] if matches else None

    def terminal(self, run, info):
        address = info['address']
        labels = info.get('labels') or {}
        space_id, tab_id = labels.get('herdr.space'), labels.get('herdr.tab')
        for space in self.spaces():
            for tab in space.get('tabs', []):
                if any(terminal.get('terminal') == address for terminal in tab.get('terminals', [])):
                    space_id, tab_id = space['id'], tab['id']
        return {'machineId': run['scope']['machineId'], 'address': address,
                'spaceId': space_id, 'tabId': tab_id}

    def start(self, run, argv):
        destination = run['workspace']['path']
        matching = next((space for space in self.spaces() if space.get('folder')
                         and str(Path(space['folder']).resolve()) == destination), None)
        if matching:
            result = self.app('tab.new', space=matching['id'], name=run['scope']['issueId'],
                              folder=destination, argv=argv)
            result.setdefault('space', matching['id'])
        else:
            result = self.app('space.new', name=run['scope']['issueId'], folder=destination,
                              tab=run['scope']['issueId'], machine='', argv=argv)
        if not all(result.get(key) for key in ('terminal', 'space', 'tab')):
            raise Refusal('evidence_unavailable', 'Herdr creation outcome uncertain; reconnect to the operation, never relaunch.', True)
        self.associate(run, result['terminal'])
        return result

    def focus(self, run, info):
        self.validate(run, info)
        self.app('agent.focus', terminal=info['address'])

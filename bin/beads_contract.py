"""Versioned lifecycle protocol; adapter-private data never enters these schemas."""
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


class Refusal(Exception):
    def __init__(self, code, message, retryable=False, capability=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.capability = capability


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


@lru_cache(maxsize=32)
def validator(kind, definition=None):
    directory = Path(__file__).resolve().parent.parent / 'schemas/v1'
    schemas = {name: json.loads((directory / f'{name}.schema.json').read_text())
               for name in ('run', 'controller', 'adapter')}
    registry = Registry().with_resources((s['$id'], Resource.from_contents(s)) for s in schemas.values())
    schema = schemas[kind]
    if definition:
        schema = {'$ref': f"{schema['$id']}#/$defs/{definition}"}
    return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())


def validate(kind, value, definition=None):
    error = next(validator(kind, definition).iter_errors(value), None)
    if error:
        path = '.'.join(str(part) for part in error.absolute_path) or '<root>'
        raise Refusal('invalid_record' if kind == 'run' else 'invalid_request',
                      f'{kind}/{definition or "root"} at {path}: {error.message[:500]}')
    if kind == 'run' and definition in (None, 'Run'):
        validate_run(value)


def validate_run(run):
    """Compare repeated identities that JSON Schema cannot express."""
    def invalid(message):
        raise Refusal('invalid_record', message)

    def submission_key(identity):
        return (identity['submissionId'], identity['submissionRevision'])

    if run['target']['mode'] != 'github-pr' and run['target']['repository'] != run['scope']['repository']:
        invalid('Run target does not identify its canonical repository.')
    submissions = {}
    for submission in run['submissions']:
        identity = submission['identity']
        k = submission_key(identity)
        if k in submissions:
            invalid('Duplicate immutable submission revision.')
        submissions[k] = submission
        evidence = submission['evidence']
        if evidence and (submission_key(evidence['submission']) != k or evidence['head'] != identity['head']
                         or evidence['target'] != identity['target'] or evidence['kind'] != identity['target']['mode']):
            invalid('Stored evidence differs from its submission identity.')
        if evidence and evidence['kind'] == 'github-pr' and (
                evidence['pullRequest'] != identity['pullRequest'] or evidence['mergedHead'] != identity['head']):
            invalid('Stored PR evidence differs from the exact PR/head.')
        if evidence and evidence['kind'] == 'local-target' and evidence['receipt']:
            receipt = evidence['receipt']
            if (receipt['fence'] != evidence['fence'] or receipt['submission'] != evidence['submission']
                    or receipt['head'] != identity['head'] or receipt['target'] != identity['target']
                    or receipt['attestedBy'] != run['owner']):
                invalid('Stored integration receipt differs from its exact evidence.')
    required = [submission_key(k) for k in run['coverage']['required']]
    if any(k not in submissions or submissions[k]['identity']['target'] != run['target'] for k in required):
        invalid('Required coverage does not resolve to the current exact target/submission.')
    if run['coverage']['state'] == 'integrated' and (
            run['disposition'] != 'current' or any(submissions[k]['state'] != 'integrated'
            or not submissions[k]['evidence'] or submissions[k]['evidence']['fence'] != run['fence'] for k in required)):
        invalid('Integrated coverage requires current-fence proof for every required submission.')
    if run['closure']['state'] == 'awaiting_manual':
        ids = [submissions[k]['evidence']['evidenceId'] for k in required if submissions[k]['evidence']]
        if run['coverage']['state'] != 'integrated' or run['closure']['evidenceIds'] != ids or not run['closure']['manualCommand']:
            invalid('Manual handoff requires complete current evidence.')
    elif run['closure']['manualCommand'] is not None:
        invalid('A closure command is only valid for the verified manual handoff.')
    if 'manual_cleanup_required' not in run['cleanup']['blockers']:
        invalid('Retained resources require a visible manual-cleanup reason.')

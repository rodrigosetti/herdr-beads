"""Herdr display labels, independent of Git, workspace and session identity."""
from pathlib import Path


def issue_space_name(issue):
    return issue['title']


def reconcile_space_names(issue, workspace_path, spaces, app):
    destination = Path(workspace_path).resolve()
    name = issue_space_name(issue)
    renamed = []
    for space in spaces:
        if (space.get('machine') or not space.get('folder')
                or Path(space['folder']).resolve() != destination):
            continue
        if space.get('name') != name:
            app('space.rename', id=space['id'], name=name)
            renamed.append(space['id'])
    return renamed


def reconcile_project_spaces(project, hosting, machine):
    """Refresh legacy associations too, without adopting or resuming sessions.

    Labels discover candidates only. The current Beads session must independently
    bind this machine and the same exact workspace as the terminal and space.
    Finished terminals remain candidates so idle/stopped sessions stay named.
    """
    spaces = hosting.spaces()
    folders = {str(Path(space['folder']).resolve()) for space in spaces
               if not space.get('machine') and space.get('folder')}
    result = {'renamed': [], 'errors': []}
    if not folders:
        return result
    rows = hosting.call(['hc', 'ls', '--all', '--json', '--label', f'beads.project={project.beads}']).get('terminals')
    if not isinstance(rows, list):
        raise ValueError('Herdr did not return a terminal list for display reconciliation.')
    candidates = {}
    for row in rows:
        address = row.get('address', '')
        if not address or '/' in address or not row.get('cwd') or str(Path(row['cwd']).resolve()) not in folders:
            continue
        try:
            info = hosting.call(['hc', 'get', address, '--json'])
            labels = info.get('labels') or {}
            issue_id = labels.get('beads.issue')
            if labels.get('beads.project') != str(project.beads) or not issue_id:
                continue
            cwd = info.get('cwd') or (info.get('foreground') or {}).get('cwd')
            if cwd:
                candidates.setdefault(issue_id, set()).add(str(Path(cwd).resolve()))
        except Exception as error:
            result['errors'].append({'terminal': address, 'error': str(error)})
    for issue_id, paths in candidates.items():
        try:
            issues = project.bd('show', issue_id)
            if not isinstance(issues, list) or len(issues) != 1 or issues[0].get('id') != issue_id:
                raise ValueError(f'Expected the full exact issue {issue_id} for display reconciliation.')
            issue = issues[0]
            session = (issue.get('metadata') or {}).get('omp_session')
            if not isinstance(session, dict) or session.get('machine') != machine:
                continue
            path = session.get('worktree')
            if (not isinstance(path, str) or not Path(path).is_absolute()
                    or str(Path(path).resolve()) != path or path not in paths):
                continue
            result['renamed'].extend(reconcile_space_names(issue, path, spaces, hosting.app))
        except Exception as error:
            result['errors'].append({'issue': issue_id, 'error': str(error)})
    return result

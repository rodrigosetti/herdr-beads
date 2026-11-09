"""Session-independent epic policy over the project's parsed-JSON bd adapter.

Beads 1.3.2's ``epic close-eligible`` calls unchecked CloseIssue, bypassing
normal close gates. Use unforced ``close`` instead: its transaction checks open
children and live blockers; its CLI also checks pins, ownership and gates.

The complete predicate is NOT atomic: bd exposes no transaction/CAS covering
nonempty membership plus child statuses plus the parent transition. A concurrent
last-child removal, child reopen or reparent can invalidate a read. We re-read
and repair closed invalid epics, but another mutation can occur after the last
read. Periodic reconciliation is required; this is convergence, not a database
invariant. Normal bd close's CLI-only preflight gates have their own read/write
window. Never replace these operations with force, raw SQL or unchecked closure.
"""


class EpicReconcileError(RuntimeError):
    """The bd adapter returned data that cannot safely drive epic policy."""


_LIST_FLAGS = (
    "--all", "--limit", "0", "--flat", "--include-ephemeral",
    "--include-gates", "--include-infra", "--include-templates",
)


def _rows(value):
    if not isinstance(value, list) or any(
        not isinstance(row, dict) or not row.get("id") or not row.get("status")
        for row in value
    ):
        raise EpicReconcileError("Expected bd to return an issue array with IDs and statuses")
    return value


def _show(project, issue_id):
    rows = _rows(project.bd("show", issue_id))
    if len(rows) != 1 or rows[0]["id"] != issue_id:
        raise EpicReconcileError(f"Expected exactly one issue for {issue_id!r}")
    return rows[0]


def _children(project, issue_id):
    # --parent also matches dotted IDs without a parent edge. Only the actual
    # parent field identifies membership; --flat avoids tree expansion.
    rows = _rows(project.bd("list", "--parent", issue_id, *_LIST_FLAGS))
    return [row for row in rows if row.get("parent") == issue_id]


def _progress(issue, children):
    total = len(children)
    closed = sum(child["status"] == "closed" for child in children)
    return {
        "id": issue["id"], "total_children": total, "closed_children": closed,
        "status": issue["status"], "eligible": total > 0 and closed == total,
    }


def reconcile_epics(project):
    """Reconcile actual direct children bottom-up; return progress and refusals.

    ``project.bd(*args)`` must return parsed bd JSON and raise its
    ``project.bd_error_type`` on CLI failure (defaults to RuntimeError).
    Only epic lifecycle status changes are requested; metadata/session records,
    child statuses and unrelated tasks are never written by this module.

    ``epics`` includes closed and empty epics. A row's optional ``held`` contains
    a failed mutation/read diagnostic; other epics continue independently.
    ``closed``/``reopened`` record successful commands, not an atomic snapshot.
    ``errors`` is an array of ``{id, error}`` diagnostics. Initial enumeration
    failure propagates: there is no trustworthy progress to return. Call
    periodically in the current runtime; the eventual controller service should
    call the same policy.
    """
    epics = _rows(project.bd("list", "--type", "epic", *_LIST_FLAGS))
    result = {"epics": [], "closed": [], "reopened": [], "errors": []}
    errors = {}
    failure_types = (getattr(project, "bd_error_type", RuntimeError), EpicReconcileError, OSError)
    known = {issue["id"]: issue for issue in epics}
    completed = set()
    visiting = set()
    progress = {}

    def hold(issue_id, error):
        errors[issue_id] = str(error)

    def reopen(issue_id):
        # Do not overwrite another actor's transition away from closed.
        project.bd("update", issue_id, "--if-status", "closed", "--status", "open")
        result["reopened"].append(issue_id)

    def visit(issue_id):
        if issue_id in completed:
            return
        if issue_id in visiting:
            raise EpicReconcileError(f"Parent-child cycle involving epic {issue_id}")
        visiting.add(issue_id)
        issue = known[issue_id]
        children = None
        attempted_reopen = False
        try:
            children = _children(project, issue_id)
            for child in sorted(children, key=lambda row: row["id"]):
                if child.get("issue_type") == "epic":
                    known.setdefault(child["id"], child)
                    visit(child["id"])
            # Descendant transitions may have changed these direct children.
            issue = _show(project, issue_id)
            children = _children(project, issue_id)
            if issue.get("issue_type") != "epic":
                raise EpicReconcileError(f"{issue_id} is no longer an epic; no mutation requested")
            row = _progress(issue, children)
            if issue["status"] == "closed" and not row["eligible"]:
                attempted_reopen = True
                reopen(issue_id)
            elif issue["status"] != "closed" and row["eligible"]:
                # Unlike close-eligible, this preserves ordinary bd close gates.
                project.bd("close", issue_id, "--reason", "All direct children closed")
                result["closed"].append(issue_id)
        except failure_types as error:
            hold(issue_id, error)
        finally:
            visiting.remove(issue_id)
            completed.add(issue_id)

        # Re-read even after a rejected command: retain visible progress and
        # repair a predicate invalidated during close, without retrying closure.
        try:
            issue = _show(project, issue_id)
            children = _children(project, issue_id)
            row = _progress(issue, children)
            if (not attempted_reopen and issue.get("issue_type") == "epic"
                    and issue["status"] == "closed" and not row["eligible"]):
                reopen(issue_id)
                issue = _show(project, issue_id)
                children = _children(project, issue_id)
                row = _progress(issue, children)
            progress[issue_id] = row
        except failure_types as error:
            hold(issue_id, error)
            if children is not None:
                progress[issue_id] = _progress(issue, children)
            else:
                progress[issue_id] = {
                    "id": issue_id, "status": issue["status"],
                    "total_children": None, "closed_children": None, "eligible": False,
                }

    for issue_id in sorted(known):
        visit(issue_id)
    for issue_id in sorted(progress):
        row = progress[issue_id]
        if issue_id in errors:
            row["held"] = errors[issue_id]
        result["epics"].append(row)
    result["errors"] = [{"id": issue_id, "error": errors[issue_id]} for issue_id in sorted(errors)]
    return result

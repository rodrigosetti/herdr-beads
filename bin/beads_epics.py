"""Session-independent, read-only epic progress for service and legacy dashboard.

All closure is owner-reviewed manual work. Child completion is information, not
permission for background close/reopen operations, retries, or force bypasses.
"""


class EpicReconcileError(RuntimeError):
    """The bd adapter returned data that cannot safely describe epic progress."""


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


def observe_epics(project):
    """Return direct-child progress and per-epic errors, never status mutations.

    project.bd(*args) returns parsed JSON and raises project.bd_error_type on
    failure (RuntimeError by default). Only list calls are made. An initial
    enumeration failure propagates rather than presenting a false empty snapshot.
    """
    result = {"epics": [], "errors": []}
    for issue in _rows(project.bd("list", "--type", "epic", *_LIST_FLAGS)):
        try:
            # --parent also matches dotted IDs without a parent edge. Only the
            # actual parent field identifies membership; --flat avoids expansion.
            children = [row for row in _rows(project.bd("list", "--parent", issue["id"], *_LIST_FLAGS))
                        if row.get("parent") == issue["id"]]
            closed = sum(child["status"] == "closed" for child in children)
            eligible = bool(children) and closed == len(children)
            result["epics"].append({"id": issue["id"], "total_children": len(children),
                "closed_children": closed, "status": issue["status"], "eligible": eligible,
                "reason": "All direct children closed — owner review required" if eligible else "Waiting for direct children"})
        except (getattr(project, "bd_error_type", RuntimeError), EpicReconcileError, OSError) as error:
            result["errors"].append({"id": issue["id"], "error": str(error)})
    return result

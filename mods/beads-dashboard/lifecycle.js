const executionLabels = {
  preparing: "Preparing", starting: "Starting", running: "Working", idle: "Idle",
  stopped: "Stopped", failed: "Failed", released: "Released",
};

const requiredSubmissions = (run) => (run.submissions ?? []).filter(({ identity }) =>
  run.coverage?.required?.some((key) => key.submissionId === identity.submissionId && key.submissionRevision === identity.submissionRevision));

export function lifecycleLabel(response) {
  const run = response?.run;
  if (!run) return "Not started";
  if (run.closure?.state === "observed_closed") {
    return run.coverage?.state === "integrated" ? "Externally closed — merge verified" : "Externally closed — merge unverified";
  }
  if (run.disposition && run.disposition !== "current") return `Retained — ${run.disposition.replace(/_/g, " ")}`;
  if (run.closure?.state === "awaiting_manual" && run.coverage?.state === "integrated") return "Merged — awaiting manual closure";
  if (run.coverage?.state === "integration_needs_confirmation" || requiredSubmissions(run).some(({ state }) => state === "needs_confirmation")) return "Integration needs confirmation";
  if (run.coverage?.required?.length && run.coverage.state !== "integrated") {
    if (run.target?.mode === "github-pr") return "Awaiting PR merge";
    if (run.target?.mode === "remote-target") return "Awaiting remote integration";
    return "Awaiting local integration";
  }
  return executionLabels[run.execution?.state] ?? "Execution unknown";
}

export function targetLabel(target) {
  if (!target) return "No target recorded";
  const repository = target.repository;
  const identity = target.mode === "github-pr"
    ? `${repository?.host}/${repository?.owner}/${repository?.name} (repository ID ${repository?.repositoryId})`
    : `${repository?.root} (common directory ${repository?.commonDir})`;
  return `${target.mode} · ${identity}${target.remoteUrl ? ` · ${target.remoteUrl}` : ""} · ${target.ref}`;
}

export function manualClosureCommand(response) {
  const run = response?.run;
  if (run?.disposition !== "current" || run.coverage?.state !== "integrated" || run.closure?.state !== "awaiting_manual") return null;
  return typeof run.closure.manualCommand === "string" && run.closure.manualCommand.length ? run.closure.manualCommand : null;
}

// Every controller-provided string stays inside a fence longer than any embedded run.
function fenced(value, language = "json") {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? null, null, 2);
  const length = Math.max(3, ...(text.match(/`+/g) ?? []).map((run) => run.length + 1));
  const fence = "`".repeat(length);
  return `${fence}${language}\n${text}\n${fence}`;
}

export function lifecycleMarkdown(response, { available = true } = {}) {
  const run = response?.run;
  const sections = ["## Lifecycle", lifecycleLabel(response)];
  if (run) {
    sections.push(
      "### Exact scope", fenced(run.scope),
      "### Owner and fence", fenced({ owner: run.owner, fence: run.fence, revision: run.revision, disposition: run.disposition, createdAt: run.createdAt, updatedAt: run.updatedAt }),
      "### Integration target", fenced(targetLabel(run.target), "text"), fenced(run.target),
      "### Execution (not attention)", fenced(run.execution),
      "### Required coverage and blockers", fenced({ coverage: run.coverage, blockers: requiredSubmissions(run).filter(({ state }) => state !== "integrated").map(({ identity, state, observedAt }) => ({ submissionId: identity.submissionId, submissionRevision: identity.submissionRevision, state, observedAt })), closure: { state: run.closure?.state, reason: run.closure?.reason, evidenceIds: run.closure?.evidenceIds, observedAt: run.closure?.observedAt } }),
      "### Submissions, exact heads, PRs and evidence", fenced(run.submissions),
      "### Observed Beads issue", "Timestamped observation only; external closure does not establish integration or controller closure ownership.", fenced(run.issueObservation),
      "### Cleanup — resources retained", "Automatic cleanup is disabled, including after merge, manual closure or explicit owner release. No workspace, branch, registration, transcript, session, space or terminal is removed.", fenced({ state: run.cleanup?.state, blockers: run.cleanup?.blockers, checkedAt: run.cleanup?.checkedAt, release: run.release }),
      "### Retention reason", fenced(run.cleanup?.reason ?? "Automatic cleanup is disabled; resources require separate operator review.", "text"),
      "### Workspace observation", fenced(run.cleanup?.workspaceObservation ?? { state: "unknown", reason: "No provider observation recorded; operator review required.", observedAt: null }),
      "Missing means observed absence only, not cleanup proof; registrations and other resources remain retained. Unknown or unavailable provider observations require review. Neither observation nor explicit release authorizes cleanup.",
      "An operator may separately review current ownership/activity, tracked, untracked and ignored work, stashes, nested repositories and unpreserved commits, then remove resources externally under the ordinary provider workflow. No cleanup command or action is supplied here.",
      "### Canonical workspace (recorded identity)", fenced(run.workspace),
    );
  }
  sections.push("### Durable operation", fenced(response?.operation), "### Controller result and capabilities", fenced({ outcome: response?.outcome, action: response?.action, error: response?.error ?? null, actions: response?.actions ?? [], support: response?.support ?? null }));
  sections.push(
    "### Trusted integration receipt review",
    "The socket cannot attest human review: it always calls Controller.handle with trusted_human=False and refuses confirm_integration with untrusted_receipt. This dashboard has no usable confirmation action and cannot grant that authority.",
    "An explicitly owner-authorized trusted local embedding may call Controller.handle(request, trusted_human=True) only after owner review. The request must use the current expected fence and revision; its receipt must bind the exact current fence (run ID, attempt, generation), required submission ID and revision, exact head SHA, exact local target (repository and ref), resulting target commit, owner attestedBy, receiptId and attestedAt. Then reconcile: the observer must independently verify that exact resulting commit on the recorded target. Recording a receipt alone is not integration proof.",
  );
  const command = available ? manualClosureCommand(response) : null;
  if (command) {
    sections.push("### Manual closure — copy only", "Owner review required. This normal bd command is not executed by the dashboard and has no controller fence/revision concurrency guard. Refresh and recheck the issue owner, current run, exact target and complete evidence immediately before manually running it; concurrent reassignment, reopen, new submissions or target changes can make a copied command stale.", fenced(command, "sh"));
  }
  return sections.join("\n\n");
}

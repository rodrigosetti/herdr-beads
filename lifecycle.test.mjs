import test from "node:test";
import assert from "node:assert/strict";
import { lifecycleLabel, lifecycleMarkdown, manualClosureCommand, targetLabel } from "./mods/beads-dashboard/lifecycle.js";

const target = { mode: "local-target", repository: { root: "/repo", commonDir: "/repo/.git" }, ref: "refs/heads/main" };
const identity = { submissionId: "submission-1", submissionRevision: 2, head: "a".repeat(40), target, pullRequest: null };
function response(overrides = {}) {
  return {
    apiVersion: 1, outcome: "ok", action: "status", actions: [], operation: null,
    run: {
      scope: { repository: target.repository, beadsDir: "/repo/.beads", issueId: "full-issue.1", machineId: "machine-1" },
      owner: "owner-1", fence: { runId: "run-1", attempt: 2, generation: 3 }, revision: 9, disposition: "current",
      target, execution: { state: "running", activity: "active", access: "writable", session: null, terminal: null },
      coverage: { state: "unsubmitted", required: [], workspaceHead: null, revision: 0 }, submissions: [],
      closure: { state: "not_requested", manualCommand: null, evidenceIds: [], reason: null },
      cleanup: { state: "held", blockers: ["manual_cleanup_required", "owner_active"] }, release: { state: "held", receipt: null },
      workspace: { path: "/worktree", branchRef: "refs/heads/issue-branch", provider: "omp-worktree" },
      issueObservation: { status: "in_progress", assignee: "owner-1", observedAt: "2026-01-01T00:00:00Z" },
      createdAt: "2026-01-01T00:00:00Z", updatedAt: "2026-01-02T00:00:00Z", ...overrides,
    },
  };
}
function submitted(state = "pending", mode = "local-target") {
  return response({
    target: { ...target, mode },
    coverage: { state: "partial", required: [{ submissionId: identity.submissionId, submissionRevision: identity.submissionRevision }], workspaceHead: identity.head, revision: 1 },
    submissions: [{ identity, state, evidence: null, observedAt: null }],
  });
}
function integrated() {
  const value = submitted("integrated");
  value.run.coverage.state = "integrated";
  value.run.closure = { state: "awaiting_manual", manualCommand: "BEADS_DIR=/repo/.beads bd --sandbox -C /repo close --reason reviewed -- full-issue.1", evidenceIds: ["proof-1"], reason: "Merged — awaiting manual closure" };
  value.run.submissions[0].evidence = { evidenceId: "proof-1", fence: value.run.fence, submission: value.run.coverage.required[0], head: identity.head, target, kind: "local-target", targetCommit: "b".repeat(40), proof: "ancestry" };
  return value;
}

test("lifecycle summaries distinguish verified integration from observed external closure", () => {
  assert.equal(lifecycleLabel(), "Not started");
  assert.equal(lifecycleLabel({ run: null, attention: "working" }), "Not started");
  const value = integrated();
  assert.equal(lifecycleLabel(value), "Merged — awaiting manual closure");
  value.run.closure.state = "observed_closed";
  assert.equal(lifecycleLabel(value), "Externally closed — merge verified");
  for (const state of ["unknown", "partial", "pending", "unsubmitted"]) {
    value.run.coverage.state = state;
    assert.equal(lifecycleLabel(value), "Externally closed — merge unverified");
  }
});

test("incomplete required coverage identifies its integration mode and confirmation need", () => {
  assert.equal(lifecycleLabel(submitted()), "Awaiting local integration");
  assert.equal(lifecycleLabel(submitted("open", "github-pr")), "Awaiting PR merge");
  assert.equal(lifecycleLabel(submitted("pending", "remote-target")), "Awaiting remote integration");
  assert.equal(lifecycleLabel(submitted("needs_confirmation")), "Integration needs confirmation");
  const value = response({ coverage: { state: "integration_needs_confirmation", required: [] } });
  assert.equal(lifecycleLabel(value), "Integration needs confirmation");
  // An obsolete immutable revision must not override current required coverage.
  const obsolete = submitted();
  obsolete.run.submissions.push({ identity: { ...identity, submissionRevision: 1 }, state: "needs_confirmation" });
  assert.equal(lifecycleLabel(obsolete), "Awaiting local integration");
});

test("execution summaries are independent of reactive attention and process names", () => {
  for (const [state, expected] of Object.entries({ preparing: "Preparing", starting: "Starting", running: "Working", idle: "Idle", stopped: "Stopped", failed: "Failed", released: "Released" })) {
    const value = response({ execution: { state, activity: "unknown" } });
    Object.defineProperty(value, "attention", { get() { throw new Error("attention is not lifecycle"); } });
    Object.defineProperty(value.run, "metadata", { get() { throw new Error("legacy metadata is not lifecycle"); } });
    assert.equal(lifecycleLabel(value), expected);
  }
});

test("target labels preserve mode, full refs and authoritative repository identities", () => {
  const examples = [
    [target, ["local-target", "/repo", "/repo/.git", "refs/heads/main"]],
    [{ ...target, mode: "remote-target", remoteUrl: "ssh://example/repo.git" }, ["remote-target", "ssh://example/repo.git", "refs/heads/main"]],
    [{ mode: "github-pr", repository: { host: "github.example", owner: "org", name: "project", repositoryId: "canonical-42" }, ref: "refs/heads/release/full" }, ["github-pr", "github.example", "org", "project", "canonical-42", "refs/heads/release/full"]],
  ];
  for (const [value, identities] of examples) {
    const text = targetLabel(value);
    for (const identity of identities) assert.ok(text.includes(identity), identity);
  }
});

test("manual closure is only the current integrated awaiting-manual controller command", () => {
  const value = integrated();
  assert.equal(manualClosureCommand(value), value.run.closure.manualCommand);
  assert.ok(!lifecycleMarkdown(value, { available: false }).includes(value.run.closure.manualCommand));
  for (const disposition of ["superseded", "reassigned", "reopened", "cancelled"]) {
    const stale = integrated();
    stale.run.disposition = disposition;
    assert.equal(lifecycleLabel(stale), `Retained — ${disposition}`);
    assert.equal(manualClosureCommand(stale), null);
    assert.ok(!lifecycleMarkdown(stale).includes(stale.run.closure.manualCommand));
  }
  for (const state of ["pending", "partial", "unknown", "unsubmitted"]) {
    const stale = integrated();
    stale.run.coverage.state = state;
    assert.equal(manualClosureCommand(stale), null);
    assert.ok(!lifecycleMarkdown(stale).includes(stale.run.closure.manualCommand));
  }
  for (const state of ["held", "observed_closed", "not_requested"]) {
    const stale = integrated();
    stale.run.closure.state = state;
    assert.equal(manualClosureCommand(stale), null);
  }
  assert.equal(manualClosureCommand(), null);
  assert.equal(manualClosureCommand({ run: null }), null);
  value.run.closure.manualCommand = null;
  assert.equal(manualClosureCommand(value), null);
});

test("lifecycle markdown exposes exact scope, durable operation, evidence and retention without closure execution", () => {
  const value = integrated();
  value.operation = { operationId: "operation-42", state: "failed", phase: "verifying", error: { code: "evidence_invalid", message: "Target changed" } };
  value.error = { code: "stale_revision", message: "Review current run" };
  const markdown = lifecycleMarkdown(value);
  for (const detail of ["/repo/.beads", "full-issue.1", "machine-1", "owner-1", "run-1", '"attempt": 2', '"generation": 3', '"revision": 9', "refs/heads/main", "submission-1", identity.head, "proof-1", "b".repeat(40), "manual_cleanup_required", "owner_active", "/worktree", "refs/heads/issue-branch", "operation-42", "failed", "evidence_invalid", "Target changed", "stale_revision", "in_progress", "2026-01-01T00:00:00Z"]) assert.ok(markdown.includes(detail), detail);
  assert.ok(markdown.includes(value.run.closure.manualCommand));
});

test("workspace observations preserve recorded identity and do not authorize closure", () => {
  for (const state of ["present", "missing", "unknown"]) {
    const value = response({
      workspace: { provider: "omp-worktree", backing: "linked", path: "/canonical/worktree", branchRef: "refs/heads/full-issue.1", sourceRepository: target.repository, commonDir: "/repo/.git" },
      cleanup: {
        state: "held", blockers: ["manual_cleanup_required"],
        reason: "retention-policy-for-this-run",
        workspaceObservation: { state, reason: `provider-observation-${state}`, observedAt: "2026-11-10T10:00:00Z" },
      },
      closure: { state: "observed_closed", manualCommand: null },
      release: { state: "released", receipt: null },
    });
    const before = structuredClone(value);
    const markdown = lifecycleMarkdown(value);
    const records = [...markdown.matchAll(/^(`{3,})json\n([\s\S]*?)\n\1$/gm)].map((match) => JSON.parse(match[2]));
    assert.ok(records.some((record) => JSON.stringify(record) === JSON.stringify(value.run.workspace)));
    assert.ok(records.some((record) => JSON.stringify(record) === JSON.stringify(value.run.cleanup.workspaceObservation)));
    assert.equal(manualClosureCommand(value), null);
    assert.deepEqual(value, before);
  }
});

test("PR identity, exact head and provider evidence remain inspectable", () => {
  const value = submitted("open", "github-pr");
  value.run.submissions[0].identity = { ...identity, pullRequest: { number: 17, url: "https://github.example/org/repo/pull/17" } };
  value.run.submissions[0].evidence = { mergedHead: identity.head, mergeCommit: "c".repeat(40), providerRecordId: "provider-proof-17" };
  const markdown = lifecycleMarkdown(value);
  for (const detail of ["https://github.example/org/repo/pull/17", '"number": 17', identity.head, "c".repeat(40), "provider-proof-17"]) assert.ok(markdown.includes(detail));
});


test("untrusted strings cannot terminate markdown fences or create active markdown", () => {
  const value = integrated();
  const hostile = "```\n# forged heading\n[execute](javascript:bad)\n``````\n<script>bad</script>";
  value.run.owner = hostile;
  value.run.target.ref = hostile;
  value.run.closure.manualCommand = hostile;
  value.operation = { operationId: hostile, state: "failed", error: { message: hostile } };
  const markdown = lifecycleMarkdown(value);
  let fence = null;
  for (const line of markdown.split("\n")) {
    const marker = /^(\x60{3,})(.*)$/.exec(line);
    if (fence) {
      if (marker && marker[1].length >= fence.length && !marker[2].trim()) fence = null;
    } else if (marker) fence = marker[1];
    else assert.ok(!line.includes("forged heading") && !line.includes("javascript:bad") && !line.includes("<script>"), line);
  }
  assert.equal(fence, null);
});

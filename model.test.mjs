import test from "node:test";
import assert from "node:assert/strict";
import { issueAction, issueRows, issueSessions } from "./mods/beads-dashboard/model.js";

const issue = (id, priority, parent, extra = {}) => ({ id, title: id, priority, parent, status: "open", issue_type: parent ? "task" : "epic", ...extra });

test("actual parents determine nesting and siblings sort by priority", () => {
  const data = [issue("unrelated-id", 2, "milestone"), issue("urgent", 0, "milestone"), issue("milestone", 1)];
  assert.deepEqual(issueRows(data, new Set()).rows.map(({ issue, depth }) => [issue.id, depth]), [["milestone", 0], ["urgent", 1], ["unrelated-id", 1]]);
});

test("Ready includes authoritative ready IDs and in-progress work, but not merely unblocked issues", () => {
  const data = [
    issue("ready", 0),
    issue("also-ready", 1),
    issue("unblocked-but-not-ready", 2),
    issue("working", 3, null, { status: "in_progress" }),
    issue("deferred", 3, null, { status: "deferred" }),
    issue("future", 3, null, { defer_until: "2099-01-01T00:00:00Z" }),
    issue("blocked", 3, null, { status: "blocked" }),
    issue("inherited-blocker", 3),
  ];
  for (const ready of [new Set(["ready", "also-ready"]), new Set(["ready", "also-ready", "working"])]) {
    const result = issueRows(data, ready, { mode: "ready" });
    assert.deepEqual(result.rows.map(({ issue }) => issue.id), ["ready", "also-ready", "working"]);
    assert.equal(result.count, 3);
    assert.deepEqual(result.counts, { all: 8, ready: 3 });
  }
});

test("all open is the default and includes every nonclosed status regardless of readiness", () => {
  const data = [
    issue("open", 0),
    issue("working", 1, null, { status: "in_progress" }),
    issue("blocked", 2, null, { status: "blocked" }),
    issue("deferred", 3, null, { status: "deferred" }),
    issue("future", 3, null, { defer_until: "2099-01-01T00:00:00Z" }),
    issue("closed", 4, null, { status: "closed" }),
  ];
  const ready = new Set(["open"]);
  const expected = ["open", "working", "blocked", "deferred", "future"];
  for (const options of [{}, { mode: "all" }]) {
    const result = issueRows(data, ready, options);
    assert.deepEqual(result.rows.map(({ issue }) => issue.id), expected);
    assert.equal(result.count, 5);
    assert.deepEqual(result.counts, { all: 5, ready: 2 });
  }
});

test("ready descendants keep their actual ancestors as uncounted context", () => {
  const data = [
    issue("closed-parent", 0, null, { status: "closed" }),
    issue("unrelated-prefix", 1, "closed-parent", { status: "in_progress" }),
    issue("ready-child", 2, "unrelated-prefix"),
    issue("not-ready-sibling", 3, "unrelated-prefix"),
    issue("closed-unrelated", 0, null, { status: "closed" }),
  ];
  const result = issueRows(data, new Set(["ready-child"]), { mode: "ready" });
  assert.equal(result.count, 2);
  assert.deepEqual(result.counts, { all: 3, ready: 2 });
  assert.deepEqual(result.rows.map(({ issue, depth, context }) => [issue.id, depth, context]), [
    ["closed-parent", 0, true],
    ["unrelated-prefix", 1, false],
    ["ready-child", 2, false],
  ]);
});

test("fetched context and closed issues never become filter matches", () => {
  const data = [
    issue("ancestor", 0, null, { context: true }),
    issue("child", 1, "ancestor"),
    issue("closed", 2, null, { status: "closed" }),
  ];
  const ready = new Set(["ancestor", "child", "closed", "missing"]);
  for (const mode of ["all", "ready"]) {
    const result = issueRows(data, ready, { mode });
    assert.equal(result.count, 1);
    assert.deepEqual(result.counts, { all: 1, ready: 1 });
    assert.deepEqual(result.rows.map(({ issue, context }) => [issue.id, context]), [["ancestor", true], ["child", false]]);
  }
});

test("folded descendants do not reappear as roots", () => {
  const data = [issue("epic", 0), issue("child", 1, "epic"), issue("grandchild", 0, "child")];
  const result = issueRows(data, new Set(), { folded: new Set(["epic"]) });
  assert.deepEqual(result.rows.map(({ issue }) => issue.id), ["epic"]);
  assert.equal(result.count, 3);
});

test("folding ready-filter context hides descendants without changing ready counts", () => {
  const data = [
    issue("epic", 0),
    issue("child", 1, "epic"),
    issue("grandchild", 0, "child"),
    issue("sibling", 2, "epic"),
  ];
  const ready = new Set(["grandchild", "sibling"]);
  const result = issueRows(data, ready, { mode: "ready", folded: new Set(["child"]) });
  assert.deepEqual(result.rows.map(({ issue, depth, context, children, folded }) => [issue.id, depth, context, children, folded]), [
    ["epic", 0, true, 2, false],
    ["child", 1, true, 1, true],
    ["sibling", 1, false, 0, false],
  ]);
  assert.equal(result.count, 2);
  assert.deepEqual(result.counts, { all: 4, ready: 2 });
  const unfolded = issueRows(data, ready, { mode: "ready" });
  assert.deepEqual(unfolded.rows.map(({ issue }) => issue.id), ["epic", "child", "grandchild", "sibling"]);
});

test("navigation reveals filtered or closed targets and ancestors without changing filter counts", () => {
  const data = [
    issue("epic", 0),
    issue("ready", 1, "epic"),
    issue("blocked", 2, "epic", { status: "blocked" }),
    issue("closed", 3, "epic", { status: "closed", context: true }),
  ];
  for (const reveal of ["blocked", "closed"]) {
    const result = issueRows(data, new Set(["ready"]), { mode: "ready", reveal });
    assert.deepEqual(result.rows.map(({ issue, depth, context }) => [issue.id, depth, context]), [
      ["epic", 0, true], ["ready", 1, false], [reveal, 1, true],
    ]);
    assert.equal(result.count, 1);
    assert.deepEqual(result.counts, { all: 3, ready: 1 });
    assert.deepEqual(issueRows(data, new Set(["ready"]), { mode: "ready" }).rows.map(({ issue }) => issue.id), ["epic", "ready"]);
  }
  assert.deepEqual(issueRows(data, new Set(), { reveal: "missing" }).rows.map(({ issue }) => issue.id), ["epic", "ready", "blocked"]);
});

test("closed ancestors can provide context and parent cycles cannot hide work", () => {
  const data = [issue("closed", 0, null, { status: "closed", context: true }), issue("child", 1, "closed"), issue("a", 2, "b"), issue("b", 3, "a")];
  const result = issueRows(data, new Set());
  assert.equal(result.count, 3);
  assert.deepEqual(new Set(result.rows.map(({ issue }) => issue.id)), new Set(["closed", "child", "a", "b"]));
});

test("sessions are scoped to database and machine, ignoring recovered shells", () => {
  const terminal = (address, project, name = "omp") => ({ address, state: "live", labels: { "beads.project": project, "beads.issue": "same-id" }, foreground: { name } });
  const terminals = [terminal("t1", "/one/.beads"), terminal("t2", "/two/.beads"), terminal("remote/t3", "/one/.beads"), terminal("t4", "/one/.beads", "zsh")];
  const agents = terminals.map(({ address }) => ({ address, status: "working" }));
  assert.deepEqual(issueSessions("/one/.beads", null, terminals, agents).get("same-id").map(({ address }) => address), ["t1"]);
  assert.deepEqual(issueSessions("/one/.beads", "remote", terminals, agents).get("same-id").map(({ address }) => address), ["remote/t3"]);
});

test("multiple live sessions preserve attention precedence and exclude finished terminals", () => {
  const statuses = ["idle", "working", "done", "blocked"];
  const terminals = statuses.map((status) => ({ address: status, state: "live", foreground: { name: "omp" }, labels: { "beads.project": "/p/.beads", "beads.issue": "p-1" } }));
  terminals.push({ ...terminals[0], address: "ended", state: "finished" });
  const sessions = issueSessions("/p/.beads", null, terminals, statuses.map((status) => ({ address: status, status })));
  assert.deepEqual(sessions.get("p-1").map(({ status }) => status), ["blocked", "done", "working", "idle"]);
});

test("live sessions take precedence over saved sessions and launch restrictions", () => {
  const saved = { omp_session: { session_id: "actual-id", session_file: "/sessions/actual.jsonl", machine: "local", worktree: "/worktree" } };
  for (const status of ["open", "in_progress", "blocked", "deferred", "closed"]) {
    const item = issue("work", 1, null, { status, metadata: saved });
    assert.equal(issueAction(item, { session: { address: "t1" }, blocked: true, context: true }), "focus");
  }
});

test("only unblocked noncontext in-progress issues with a saved key offer Resume", () => {
  const saved = { omp_session: { session_id: "actual-id", session_file: "/sessions/actual.jsonl", machine: "local", worktree: "/worktree" } };
  const working = issue("work", 1, null, { status: "in_progress", metadata: saved });
  assert.equal(issueAction(working), "resume");
  assert.equal(issueAction(working, { blocked: true }), null);
  assert.equal(issueAction(working, { context: true }), null);
  assert.equal(issueAction({ ...working, context: true }), null);
  for (const status of ["blocked", "deferred", "closed"]) {
    assert.equal(issueAction({ ...working, status }), null);
  }
  assert.equal(issueAction({ ...working, status: "open" }), "start");
  for (const metadata of [undefined, null, {}, { unrelated: "preserved" }]) {
    assert.equal(issueAction({ ...working, metadata }), "start");
  }
});

test("malformed saved records still offer Resume so the launcher can report their errors", () => {
  for (const omp_session of [null, false, 0, "", "old-id", [], {}, { session_id: "incomplete" }]) {
    const working = issue("work", 1, null, { status: "in_progress", metadata: { unrelated: "preserved", omp_session } });
    assert.equal(issueAction(working), "resume");
  }
});

test("finished and recovered terminals fall back to the persistent session rather than focus", () => {
  const working = issue("work", 1, null, { status: "in_progress", metadata: { omp_session: { session_id: "saved" } } });
  const terminal = { address: "t1", state: "live", foreground: { name: "omp" }, labels: { "beads.project": "/p/.beads", "beads.issue": working.id } };
  for (const change of [{ state: "finished" }, { foreground: { name: "zsh" } }]) {
    const sessions = issueSessions("/p/.beads", null, [{ ...terminal, ...change }], [{ address: "t1", status: "working" }]);
    assert.equal(issueAction(working, { session: sessions.get(working.id)?.[0] }), "resume");
  }
  const sessions = issueSessions("/p/.beads", null, [terminal], [{ address: "t1", status: "working" }]);
  assert.equal(issueAction(working, { session: sessions.get(working.id)?.[0] }), "focus");
});

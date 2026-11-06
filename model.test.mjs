import test from "node:test";
import assert from "node:assert/strict";
import { issueRows, issueSessions } from "./mods/beads-dashboard/model.js";

const issue = (id, priority, parent, extra = {}) => ({ id, title: id, priority, parent, status: "open", issue_type: parent ? "task" : "epic", ...extra });

test("actual parents determine nesting and siblings sort by priority", () => {
  const data = [issue("unrelated-id", 2, "milestone"), issue("urgent", 0, "milestone"), issue("milestone", 1)];
  assert.deepEqual(issueRows(data, new Set()).rows.map(({ issue, depth }) => [issue.id, depth]), [["milestone", 0], ["urgent", 1], ["unrelated-id", 1]]);
});

test("ready membership comes from the authoritative IDs, not merely an unblocked status", () => {
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
  const result = issueRows(data, new Set(["ready", "also-ready"]), { mode: "ready" });
  assert.deepEqual(result.rows.map(({ issue }) => issue.id), ["ready", "also-ready"]);
  assert.equal(result.count, 2);
  assert.deepEqual(result.counts, { all: 8, ready: 2 });
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
    assert.deepEqual(result.counts, { all: 5, ready: 1 });
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
  assert.equal(result.count, 1);
  assert.deepEqual(result.counts, { all: 3, ready: 1 });
  assert.deepEqual(result.rows.map(({ issue, depth, context }) => [issue.id, depth, context]), [
    ["closed-parent", 0, true],
    ["unrelated-prefix", 1, true],
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

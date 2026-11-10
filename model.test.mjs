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

test("attention requires an actual Herdr agent at a live terminal in the exact database and machine", () => {
  const terminal = (address, project, extra = {}) => ({ address, state: "live", labels: { "beads.project": project, "beads.issue": "same-id" }, ...extra });
  const terminals = [
    terminal("t1", "/one/.beads"),
    terminal("t2", "/two/.beads"),
    terminal("remote/t3", "/one/.beads"),
    terminal("shell", "/one/.beads", { foreground: { name: "zsh" } }),
    terminal("ended", "/one/.beads", { state: "finished" }),
    terminal("unobserved", "/one/.beads", { foreground: { name: "omp" }, title: "omp transcript" }),
  ];
  const agents = terminals.filter(({ address }) => address !== "unobserved").map(({ address }) => ({ address, status: "working" }));
  assert.deepEqual(issueSessions("/one/.beads", null, terminals, agents).get("same-id").map(({ address }) => address), ["shell", "t1"]);
  assert.deepEqual(issueSessions("/one/.beads", "remote", terminals, agents).get("same-id").map(({ address }) => address), ["remote/t3"]);
  assert.equal(issueSessions("/one/.beads", "other", terminals, agents).size, 0);
  assert.equal(issueSessions("/one/.beads", null, terminals, []).size, 0);
});

test("attention never inspects foreground or transcript to infer an execution harness", () => {
  const terminal = {
    address: "t1", state: "live", labels: { "beads.project": "/p/.beads", "beads.issue": "p-1" },
    get foreground() { throw new Error("foreground is not an authority"); },
    get transcript() { throw new Error("transcript is not an authority"); },
  };
  assert.deepEqual(issueSessions("/p/.beads", null, [terminal], [{ address: "t1", status: "blocked", title: "Needs owner" }]).get("p-1"), [
    { address: "t1", status: "blocked", title: "Needs owner", agent: true },
  ]);
});

test("multiple live sessions preserve attention precedence and exclude finished terminals", () => {
  const statuses = ["idle", "working", "done", "blocked"];
  const terminals = statuses.map((status) => ({ address: status, state: "live", labels: { "beads.project": "/p/.beads", "beads.issue": "p-1" } }));
  terminals.push({ ...terminals[0], address: "ended", state: "finished" });
  const sessions = issueSessions("/p/.beads", null, terminals, statuses.map((status) => ({ address: status, status })));
  assert.deepEqual(sessions.get("p-1").map(({ status }) => status), ["blocked", "done", "working", "idle"]);
});

const controllerResponse = (run = null, actions = [{ method: "start_or_focus", enabled: true, refusal: null }]) => ({ outcome: "ok", run, actions });
const executionRun = (activity, state = "running") => ({ disposition: "current", execution: { activity, state } });

test("Start requires explicit controller permission and eligible issue context", () => {
  const work = issue("work", 1, null, { issue_type: "task" });
  const response = controllerResponse();
  assert.equal(issueAction(work, { response }), "start");
  assert.equal(issueAction({ ...work, status: "in_progress" }, { response }), "start");
  for (const options of [{}, { response: {} }, { response: controllerResponse(null, []) }, { response, available: false }, { response, context: true }, { response, blocked: true }]) {
    assert.equal(issueAction(work, options), null);
  }
  for (const status of ["blocked", "deferred", "closed"]) assert.equal(issueAction({ ...work, status }, { response }), null);
  assert.equal(issueAction({ ...work, context: true }, { response }), null);
  assert.equal(issueAction({ ...work, issue_type: "epic" }, { response }), null);
});

test("real capability refusals never turn into fallback Start or Resume", () => {
  const work = issue("work", 1, null, { issue_type: "task", status: "in_progress", metadata: { omp_session: { session_id: "legacy" } } });
  const actions = [{ method: "start_or_focus", enabled: false, refusal: "unsupported_capability" }, { method: "resume", enabled: false, refusal: "unsupported_capability" }];
  for (const run of [null, executionRun("active"), executionRun("inactive")]) {
    assert.equal(issueAction(work, { response: { ...controllerResponse(run, actions), support: { execution: { unsupported: ["exact_resume", "durable_identity"] } } } }), null);
    assert.equal(issueAction(work, { response: { outcome: "refused", run, error: { code: "unsupported_capability", capability: "exact_resume" } } }), null);
  }
});

test("Focus and Resume use controller activity and permission, not issue metadata or attention", () => {
  const work = {
    ...issue("work", 1, null, { issue_type: "task", status: "in_progress" }),
    get metadata() { throw new Error("legacy metadata is not an authority"); },
  };
  const actions = [{ method: "start_or_focus", enabled: true }, { method: "resume", enabled: true }];
  assert.equal(issueAction(work, { response: controllerResponse(executionRun("active"), actions) }), "focus");
  for (const state of ["running", "idle", "stopped", "failed"]) {
    assert.equal(issueAction(work, { response: controllerResponse(executionRun("inactive", state), actions) }), "resume");
  }
  for (const state of ["preparing", "starting", "released"]) {
    assert.equal(issueAction(work, { response: controllerResponse(executionRun("inactive", state), actions) }), null);
  }
  assert.equal(issueAction(work, { response: controllerResponse(executionRun("unknown"), actions) }), null);
  assert.equal(issueAction(work, { response: controllerResponse({ ...executionRun("active"), disposition: "superseded" }, actions) }), null);
  assert.equal(issueAction(work, { response: controllerResponse(executionRun("active"), []), session: { address: "legacy" } }), null);
});

test("attention and legacy session metadata cannot create lifecycle actions", () => {
  for (const omp_session of [null, false, "old", {}, { session_id: "old" }]) {
    const work = issue("work", 1, null, { issue_type: "task", status: "in_progress", metadata: { omp_session } });
    assert.equal(issueAction(work, { session: { address: "live", status: "working" } }), null);
    assert.equal(issueAction(work, { response: controllerResponse() }), "start");
  }
});

test("epic progress uses authoritative direct-child counts regardless of Ready and folding", () => {
  const data = [
    issue("epic", 0),
    issue("ready-child", 1, "epic"),
    issue("nested", 2, "epic", { issue_type: "epic" }),
    issue("grandchild", 0, "nested", { status: "blocked" }),
    issue("closed-child", 3, "epic", { status: "closed" }),
  ];
  // The snapshot also includes closed children absent from the displayed issue list.
  const epics = [
    { id: "epic", total_children: 4, closed_children: 2 },
    { id: "nested", total_children: 2, closed_children: 1 },
  ];
  for (const mode of ["all", "ready"]) {
    for (const folded of [new Set(), new Set(["epic"])]) {
      const result = issueRows(data, new Set(["ready-child"]), { mode, folded, epics });
      assert.deepEqual(result.rows.find(({ issue }) => issue.id === "epic").progress, { completed: 2, total: 4 });
      assert.equal(result.count, mode === "all" ? 4 : 1);
      assert.deepEqual(result.counts, { all: 4, ready: 1 });
      assert.ok(!result.rows.some(({ issue }) => issue.id === "closed-child"));
      if (folded.has("epic")) assert.equal(result.rows.length, 1);
    }
  }
  const rows = issueRows(data, new Set(), { epics }).rows;
  assert.deepEqual(rows.find(({ issue }) => issue.id === "nested").progress, { completed: 1, total: 2 });
  assert.equal(rows.find(({ issue }) => issue.id === "ready-child").progress, null);
});

test("empty and all-closed epics retain explicit progress without exposing closed children", () => {
  const data = [
    issue("empty", 0),
    issue("complete", 1),
    issue("closed-child", 0, "complete", { status: "closed" }),
    issue("closed-unrelated", 0, null, { status: "closed" }),
  ];
  const epics = [
    { id: "empty", total_children: 0, closed_children: 0 },
    { id: "complete", total_children: 2, closed_children: 2 },
    { id: "closed-unrelated", total_children: 1, closed_children: 1 },
  ];
  const result = issueRows(data, new Set(), { epics });
  assert.deepEqual(result.rows.map(({ issue, progress }) => [issue.id, progress]), [
    ["empty", { completed: 0, total: 0 }],
    ["complete", { completed: 2, total: 2 }],
  ]);
  assert.deepEqual(result.counts, { all: 2, ready: 0 });
  assert.equal(issueRows([issue("unknown", 0)], new Set()).rows[0].progress, null);
});

test("closed epic context receives its own snapshot and reopened progress can decrease", () => {
  const data = [
    issue("closed-epic", 0, null, { status: "closed", context: true }),
    issue("reopened-child", 1, "closed-epic"),
    issue("closed-child", 2, "closed-epic", { status: "closed" }),
  ];
  for (const mode of ["all", "ready"]) {
    for (const closed_children of [2, 1]) {
      const epics = [{ id: "closed-epic", total_children: 2, closed_children }];
      const result = issueRows(data, new Set(["reopened-child"]), { mode, epics });
      assert.deepEqual(result.rows.map(({ issue, context, progress }) => [issue.id, context, progress]), [
        ["closed-epic", true, { completed: closed_children, total: 2 }],
        ["reopened-child", false, null],
      ]);
      assert.equal(result.count, 1);
      assert.deepEqual(result.counts, { all: 1, ready: 1 });
    }
  }
});

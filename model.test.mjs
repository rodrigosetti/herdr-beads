import test from "node:test";
import assert from "node:assert/strict";
import { issueRows, issueSessions } from "./mods/beads-dashboard/model.js";

const issue = (id, priority, parent, extra = {}) => ({ id, title: id, priority, parent, status: "open", issue_type: parent ? "task" : "epic", ...extra });

test("actual parents determine nesting and siblings sort by priority", () => {
  const data = [issue("unrelated-id", 2, "milestone"), issue("urgent", 0, "milestone"), issue("milestone", 1)];
  assert.deepEqual(issueRows(data, new Set()).rows.map(({ issue, depth }) => [issue.id, depth]), [["milestone", 0], ["urgent", 1], ["unrelated-id", 1]]);
});

test("inherited dependency blockers filter open issues without deleting ancestor context", () => {
  const data = [issue("epic", 0), issue("blocked", 1, "epic"), issue("available", 2, "epic")];
  const result = issueRows(data, new Set(["blocked"]), { hideBlocked: true, query: "available" });
  assert.equal(result.count, 1);
  assert.deepEqual(result.rows.map(({ issue, context }) => [issue.id, context]), [["epic", true], ["available", false]]);
});

test("folded descendants do not reappear as roots", () => {
  const data = [issue("epic", 0), issue("child", 1, "epic"), issue("grandchild", 0, "child")];
  const result = issueRows(data, new Set(), { folded: new Set(["epic"]) });
  assert.deepEqual(result.rows.map(({ issue }) => issue.id), ["epic"]);
  assert.equal(result.count, 3);
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

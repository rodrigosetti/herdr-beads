const priority = (issue) => Number.isFinite(issue.priority) ? issue.priority : 5;
const compare = (a, b) => priority(a) - priority(b) || a.id.localeCompare(b.id);

export function issueRows(issues, ready, { mode = "all", folded = new Set() } = {}) {
  const byId = new Map(issues.map((issue) => [issue.id, issue]));
  const open = issues.filter((issue) => !issue.context && issue.status !== "closed");
  const readyIssues = open.filter((issue) => ready.has(issue.id));
  const matches = mode === "ready" ? readyIssues : open;
  const matched = new Set(matches.map((issue) => issue.id));
  const included = new Set(matched);
  for (const issue of matches) {
    let parent = issue.parent;
    const seen = new Set([issue.id]);
    while (parent && byId.has(parent) && !seen.has(parent)) {
      included.add(parent);
      seen.add(parent);
      parent = byId.get(parent).parent;
    }
  }
  const children = new Map();
  const roots = [];
  for (const id of included) {
    const issue = byId.get(id);
    if (issue.parent && issue.parent !== id && included.has(issue.parent)) {
      if (!children.has(issue.parent)) children.set(issue.parent, []);
      children.get(issue.parent).push(issue);
    } else roots.push(issue);
  }
  for (const list of children.values()) list.sort(compare);
  roots.sort(compare);
  const rows = [], visited = new Set();
  function visit(issue, depth) {
    if (visited.has(issue.id)) return;
    visited.add(issue.id);
    const nested = children.get(issue.id) ?? [];
    rows.push({ issue, depth, context: !matched.has(issue.id), children: nested.length, folded: folded.has(issue.id) });
    if (folded.has(issue.id)) {
      const hide = (id) => {
        if (visited.has(id)) return;
        visited.add(id);
        for (const child of children.get(id) ?? []) hide(child.id);
      };
      for (const child of nested) hide(child.id);
    } else for (const child of nested) visit(child, depth + 1);
  }
  for (const root of roots) visit(root, 0);
  // A malformed parent cycle must not make otherwise visible issues disappear.
  for (const issue of [...included].map((id) => byId.get(id)).sort(compare)) visit(issue, 0);
  return { rows, count: matches.length, counts: { all: open.length, ready: readyIssues.length } };
}

const shells = new Set(["sh", "bash", "zsh", "fish", "nu", "dash", "ksh", "pwsh", "powershell", "cmd"]);
const urgency = { blocked: 0, done: 1, working: 2, idle: 3, starting: 4 };

export function issueSessions(beads, machine, terminals, agents) {
  const liveAgents = new Map(agents.map((agent) => [agent.address, agent]));
  const sessions = new Map();
  for (const terminal of terminals) {
    if (terminal.state !== "live" || terminal.labels?.["beads.project"] !== beads) continue;
    const address = terminal.address;
    if ((address.includes("/") ? address.slice(0, address.indexOf("/")) : null) !== (machine ?? null)) continue;
    const id = terminal.labels?.["beads.issue"];
    if (!id) continue;
    const foreground = terminal.foreground?.name?.split(/[\\/]/).pop()?.replace(/\.exe$/i, "");
    if (shells.has(foreground)) continue;
    const agent = liveAgents.get(address);
    if (!agent && foreground !== "omp") continue;
    const session = { address, status: agent?.status ?? "starting", title: agent?.title ?? terminal.title ?? "", agent: !!agent };
    if (!sessions.has(id)) sessions.set(id, []);
    sessions.get(id).push(session);
  }
  for (const list of sessions.values()) list.sort((a, b) => (urgency[a.status] ?? 5) - (urgency[b.status] ?? 5) || a.address.localeCompare(b.address));
  return sessions;
}

export const sessionLabels = { working: "Working", blocked: "Needs you", done: "Done · unseen", idle: "Idle", starting: "Starting" };
export const statusLabels = { open: "Open", in_progress: "In progress", blocked: "Blocked", deferred: "Deferred", closed: "Closed" };

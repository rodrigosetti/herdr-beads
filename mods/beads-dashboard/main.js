import { View, div } from "gpui-kit";
import { v_flex, h_flex, v_virtual_list, VirtualListScrollHandle, Scrollbar } from "gpui-base";
import { agents, exec, focused, nextCommand, panes, props, run, setShared, shared, spaces, terminals, theme } from "herdr";
import { issueAction, issueRows, issueSessions, sessionLabels, statusLabels } from "./model.js";

function preferences(key) {
  try { return JSON.parse(localStorage.getItem(key) ?? "{}"); } catch { return {}; }
}

function checked(result, label) {
  if (result.code !== 0) throw new Error((result.stderr || result.stdout || `${label} failed (${result.code})`).trim());
  return JSON.parse(result.stdout);
}

export default class BeadsDashboard extends View {
  init(_props, cx) {
    const target = props() ?? {}, focus = focused();
    const space = spaces().find((item) => item.id === focus?.space);
    const tabIds = new Set(space?.tabs.map((tab) => tab.id) ?? []);
    this.folder = target.folder || panes().find((pane) => tabIds.has(pane.tab) && pane.cwd)?.cwd || space?.folder;
    this.machine = Object.prototype.hasOwnProperty.call(target, "machine") ? target.machine : space?.machine ?? null;
    this.tab = focus?.tab;
    this.project = null;
    this.issues = [];
    this.epics = [];
    this.blocked = new Set();
    this.ready = new Set();
    this.folded = new Set();
    this.mode = "all";
    this.error = "";
    this.notice = "";
    this.loading = false;
    this.loaded = false;
    this.starting = new Set();
    this.navigationIssues = [];
    this.navigationGeneration = 0;
    this.scroll = VirtualListScrollHandle.new();
    this.rowEntities = new Map();
    this.rowSnapshot = "";
    this.actions = [];
    const dashboard = this;
    this.RowView = class extends View {
      init(row) { this.row = row; }
      update(row) { this.row = row; }
      render() { return dashboard.row(this.row, dashboard.sessionMap(), theme()); }
    };
    // Row views have their own context; process their actions in the dashboard's task.
    cx.spawn(async (cx) => {
      for (;;) {
        if (!this.actions.length) await new Promise((resolve) => { this.wakeAction = resolve; });
        while (this.actions.length) this.actions.shift()(cx);
      }
    });
    this.handle = cx.focus_handle();
    this.refresh(cx);
    cx.timer.every(10000, (cx) => this.refresh(cx));
    cx.spawn(async (cx) => {
      for (;;) {
        const command = await nextCommand();
        if (command === "focus") {
          this.tab = focused()?.tab ?? this.tab;
          this.observedSelection = null;
          this.handle.focus();
        }
        else if (command === "refresh") this.refresh(cx);
        else if (command === "filter:all" || command === "filter:ready") this.setMode(command.slice(7), cx);
        else if (command.startsWith("select:")) this.select(command.slice(7), cx);
        else if (command.startsWith("start:")) this.launch(command.slice(6), cx);
        cx.notify();
      }
    });
  }

  async bd(args) {
    return checked(await exec("env", [`BEADS_DIR=${this.project.beads}`, "bd", ...args, "--json"], { cwd: this.project.folder, on: this.machine }), "bd");
  }

  refresh(cx) {
    if (this.loading) return;
    this.loading = true;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        if (!this.project) {
          if (!this.folder) throw new Error("No project folder. Open this dashboard with go-bd --dashboard from your project.");
          const where = checked(await exec("env", ["-u", "BEADS_DIR", "bd", "where", "--json"], { cwd: this.folder, on: this.machine }), "bd where");
          const git = await exec("git", ["rev-parse", "--path-format=absolute", "--git-common-dir"], { cwd: this.folder, on: this.machine });
          const common = git.code === 0 ? git.stdout.trim().replace(/\\/g, "/") : "";
          const root = common.endsWith("/.git") ? common.slice(0, -5) : this.folder;
          this.project = { folder: root, beads: where.path, machine: this.machine };
          this.prefKey = `beads.dashboard.${JSON.stringify([this.machine, where.path])}`;
          const saved = preferences(this.prefKey);
          this.mode = saved.mode === "ready" ? "ready" : "all";
          this.folded = new Set(saved.folded ?? []);
        }
        const reconciliation = checked(await exec("go-bd", ["--reconcile-epics", "--json"], { cwd: this.project.folder, on: this.machine }), "go-bd");
        const [issues, blocked, ready] = await Promise.all([
          this.bd(["list", "--status", "open,in_progress,blocked,deferred", "--limit", "0", "--sort", "priority", "--brief"]),
          this.bd(["blocked"]),
          this.bd(["ready", "--limit", "0"]),
        ]);
        const byId = new Map(issues.map((issue) => [issue.id, issue]));
        for (const issue of ready) if (!byId.has(issue.id)) byId.set(issue.id, issue);
        const fetched = new Set();
        for (;;) {
          const missing = [...new Set([...byId.values()].map((issue) => issue.parent).filter((id) => id && !byId.has(id) && !fetched.has(id)))];
          if (!missing.length) break;
          missing.forEach((id) => fetched.add(id));
          const parents = await this.bd(["show", ...missing, "--brief-deps"]);
          for (const parent of parents) byId.set(parent.id, { ...parent, context: true });
        }
        this.issues = [...byId.values()];
        this.epics = reconciliation.epics;
        if (this.selected && !byId.has(this.selected) && !this.navigationIssues.some((issue) => issue.id === this.selected)) this.observedSelection = null;
        this.blocked = new Set(blocked.map((issue) => issue.id));
        for (const issue of issues) if (issue.status === "blocked") this.blocked.add(issue.id);
        this.ready = new Set(ready.map((issue) => issue.id));
        this.loaded = true;
        this.updated = new Date().toLocaleTimeString();
        this.error = reconciliation.errors.map(({ id, error }) => `${id}: ${error}`).join("\n");
      } catch (error) {
        this.error = String(error.message ?? error);
      } finally {
        this.loading = false;
        cx.notify();
      }
    });
  }

  save() {
    if (this.prefKey) localStorage.setItem(this.prefKey, JSON.stringify({ mode: this.mode, folded: [...this.folded] }));
  }

  setMode(mode, cx) {
    this.mode = mode;
    this.save();
    cx.notify();
  }

  ownPane() {
    const all = panes();
    return all.find((pane) => pane.mod === "beads-dashboard" && pane.focused)
      ?? all.find((pane) => pane.mod === "beads-dashboard" && pane.tab === this.tab);
  }

  synchronize(cx) {
    if (!this.project || !this.tab) return;
    const value = shared(`beads.selection.${this.tab}`);
    if (!value || value.beads !== this.project.beads || (value.machine ?? null) !== this.machine || !value.id || value.id === this.observedSelection) return;
    this.observedSelection = value.id;
    this.selected = value.id;
    this.revealPending = value.id;
    this.navigationIssues = [];
    const generation = ++this.navigationGeneration;
    if (this.issues.some((issue) => issue.id === value.id)) return;
    cx.spawn(async (cx) => {
      try {
        const found = await this.bd(["show", `--id=${value.id}`, "--brief-deps"]);
        if (!found.some((issue) => issue.id === value.id)) throw new Error(`bd show did not return issue ${value.id}.`);
        const byId = new Map(this.issues.map((issue) => [issue.id, issue]));
        for (const issue of found) byId.set(issue.id, { ...issue, context: true });
        const fetched = new Set();
        for (;;) {
          const missing = [...new Set([...byId.values()].map((issue) => issue.parent).filter((id) => id && !byId.has(id) && !fetched.has(id)))];
          if (!missing.length) break;
          missing.forEach((id) => fetched.add(id));
          for (const issue of await this.bd(["show", ...missing, "--brief-deps"])) byId.set(issue.id, { ...issue, context: true });
        }
        if (generation !== this.navigationGeneration) return;
        this.navigationIssues = [...byId.values()].filter((issue) => issue.context);
      } catch (error) {
        if (generation === this.navigationGeneration) this.error = String(error.message ?? error);
      } finally { cx.notify(); }
    });
  }

  dispatch(action) {
    this.actions.push(action);
    this.wakeAction?.();
    this.wakeAction = null;
  }

  synchronizeRows(rows, sessions) {
    const snapshot = JSON.stringify([rows, [...sessions], [...this.ready], [...this.blocked], [...this.starting], this.selected]);
    if (snapshot === this.rowSnapshot) return;
    this.rowSnapshot = snapshot;
    this.dispatch((cx) => {
      if (this.rowSnapshot !== snapshot) return;
      const present = new Set(rows.map((row) => row.issue.id));
      for (const [id, entity] of this.rowEntities) {
        if (!present.has(id)) { entity.release(); this.rowEntities.delete(id); }
      }
      for (const row of rows) {
        const entity = this.rowEntities.get(row.issue.id);
        if (entity) entity.set_props(row);
        else this.rowEntities.set(row.issue.id, cx.new(this.RowView, row));
      }
      cx.notify();
    });
  }

  close() {
    this.scroll.release();
    for (const entity of this.rowEntities.values()) entity.release();
  }

  select(id, cx) {
    if (!this.project || ![...this.issues, ...this.navigationIssues].some((issue) => issue.id === id)) return;
    const dashboard = this.ownPane();
    if (!dashboard) { this.error = "Dashboard pane not found. Open it as a tab with go-bd --dashboard."; cx.notify(); return; }
    this.tab = dashboard.tab;
    this.selected = id;
    const selection = `beads.selection.${this.tab}`;
    setShared(selection, { ...this.project, id });
    const detail = panes().find((pane) => pane.mod === "bead-detail" && pane.tab === this.tab);
    if (detail) run("pane.focus", { pane: detail.pane });
    else run("pane.split", { pane: dashboard.pane, direction: "right", mod: "bead-detail", props: { selection } });
    cx.notify();
  }

  sessionMap() {
    return this.project ? issueSessions(this.project.beads, this.machine, terminals(), agents()) : new Map();
  }

  focusSession(session) {
    run(session.agent ? "agent.focus" : "pane.focus", { terminal: session.address });
  }

  launch(id, cx) {
    const issue = this.issues.find((item) => item.id === id && !item.context);
    if (!issue || !this.project || this.starting.has(id)) return;
    const existing = this.sessionMap().get(id);
    if (existing?.length) { this.focusSession(existing[0]); return; }
    if (!issueAction(issue, { blocked: this.blocked.has(id) })) {
      this.error = issue.issue_type === "epic" ? `${id} is an epic. Start one of its child issues instead.`
        : `${id} is not available to start. Resolve its blockers or deferred status first.`;
      cx.notify();
      return;
    }
    this.starting.add(id);
    this.error = "";
    this.notice = "";
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const result = checked(await exec("go-bd", ["--herdr", id, "--json"], { cwd: this.project.folder, on: this.machine }), "go-bd");
        if (result.error) throw new Error(result.error);
        const notice = result.action === "focused" ? "Opened existing session" : result.action === "resumed" ? "Resumed session" : "Started session";
        this.notice = `${notice} for ${id}`;
        this.refresh(cx);
      } catch (error) { this.error = String(error.message ?? error); }
      finally { this.starting.delete(id); cx.notify(); }
    });
  }

  button(id, label, action, t, active = false) {
    return div().id(id).flex_none().px(10).py(6).rounded(6).border(1).border_color(active ? t.accent : t.border)
      .bg(active ? t.selected : t.surface).text_color(active ? t.foreground : t.muted).text_size(12).cursor_pointer()
      .when(active, (element) => element.font_semibold())
      .hover((style) => style.bg(t.hover).text_color(t.foreground)).on_click((_event, cx) => { cx.stop_propagation(); this.dispatch(action); }).child(label);
  }

  row(row, sessions, t) {
    const { issue, depth, context, children, folded, progress } = row;
    const live = sessions.get(issue.id) ?? [], session = live[0];
    const blocked = this.blocked.has(issue.id), ready = this.ready.has(issue.id), busy = this.starting.has(issue.id);
    const decision = issueAction(issue, { session, blocked, context });
    const priorityColor = [t.blocked, t.ansi3, t.ansi6, t.muted, t.dim][issue.priority] ?? t.dim;
    const fold = div().id(`fold-${issue.id}`).w(16).flex_none().text_color(t.dim)
      .when(children > 0, (element) => element.cursor_pointer().on_click((_event, cx) => {
        cx.stop_propagation();
        this.dispatch((cx) => {
          this.folded.has(issue.id) ? this.folded.delete(issue.id) : this.folded.add(issue.id);
          this.save(); cx.notify();
        });
      })).child(children ? folded ? "▸" : "▾" : "");
    const title = div().flex_1().min_w(0).truncate().text_size(13).text_color(context ? t.muted : t.foreground)
      .when(issue.issue_type === "epic", (element) => element.font_semibold()).tooltip(issue.title).child(issue.title);
    const info = h_flex().gap(9).items_center().text_size(11).text_color(t.dim)
      .child(div().flex_none().child(issue.id))
      .child(div().flex_none().text_color(t.muted).child(issue.issue_type))
      .child(div().flex_none().text_color(ready ? t.ansi2 : issue.status === "in_progress" ? t.working : t.muted)
        .child(ready ? "Ready" : statusLabels[issue.status] ?? issue.status))
      .when(!!progress, (element) => element.child(div().flex_none().text_color(t.muted)
        .child(`${progress.completed}/${progress.total} children closed`)))
      .when(blocked, (element) => element.child(div().text_color(t.blocked).child("Dependency blocked")))
      .when(context, (element) => element.child(div().child(this.selected === issue.id ? "Navigation context" : "Group context")));
    const action = decision === "focus" ? this.button(`go-${issue.id}`, "Go to session", () => this.focusSession(session), t)
      : busy && decision ? div().flex_none().text_size(12).text_color(t.muted).frames(decision === "resume" ? ["Resuming", "Resuming ·", "Resuming ··"] : ["Starting", "Starting ·", "Starting ··"], 300)
      : decision ? this.button(`start-${issue.id}`, decision === "resume" ? "Resume" : "Start", (cx) => this.launch(issue.id, cx), t)
      : div().flex_none().w(58);
    return h_flex().id(`bead-${issue.id}`).flex_none().h(68).pl(10 + Math.min(depth, 8) * 18).pr(12).py(10).gap(8).items_center()
      .border_b(1).border_color(t.border).cursor_pointer().bg(this.selected === issue.id ? t.selected : issue.issue_type === "epic" ? t.surface : t.background)
      .hover((style) => style.bg(t.hover)).on_click(() => this.dispatch((cx) => this.select(issue.id, cx)))
      .child(fold)
      .child(div().w(27).flex_none().text_size(11).font_semibold().text_color(priorityColor).child(`P${issue.priority ?? "?"}`))
      .child(v_flex().flex_1().min_w(0).gap(5).child(title).child(info))
      .when(!!session, (element) => element.child(div().id(`session-${issue.id}`).flex_none().text_size(11)
        .text_color(t[session.status] ?? t.muted).tooltip(live.map((item) => `${item.address}: ${sessionLabels[item.status] ?? item.status}`).join("\n"))
        .on_click((_event, cx) => { cx.stop_propagation(); this.focusSession(session); })
        .child(`${sessionLabels[session.status] ?? session.status}${live.length > 1 ? ` +${live.length - 1}` : ""}`)))
      .child(action);
  }

  render(cx) {
    this.synchronize(cx);
    const t = theme(), sessions = this.sessionMap();
    const known = new Set(this.issues.map((issue) => issue.id));
    const issues = [...this.issues, ...this.navigationIssues.filter((issue) => !known.has(issue.id))];
    if (this.revealPending && issues.some((issue) => issue.id === this.revealPending)) {
      const byId = new Map(issues.map((issue) => [issue.id, issue])), seen = new Set();
      let parent = byId.get(this.revealPending)?.parent;
      while (parent && !seen.has(parent)) {
        seen.add(parent);
        this.folded.delete(parent);
        parent = byId.get(parent)?.parent;
      }
      this.save();
    }
    const { rows, count, counts } = issueRows(issues, this.ready, { mode: this.mode, folded: this.folded, reveal: this.selected, epics: this.epics });
    this.synchronizeRows(rows, sessions);
    const index = this.revealPending ? rows.findIndex((row) => row.issue.id === this.revealPending) : -1;
    if (index >= 0) { this.scroll.scroll_to_item(index, "center"); this.revealPending = null; }
    const name = this.project?.folder.replace(/[\\/]$/, "").split(/[\\/]/).pop() || "Project";
    return v_flex().id("beads-dashboard").size_full().min_h(0).bg(t.background).text_color(t.foreground).text_size(13)
      .track_focus(this.handle).on_mouse_down("left", () => this.handle.focus())
      .child(v_flex().flex_none().p(16).gap(12).border_b(1).border_color(t.border)
        .child(h_flex().gap(10).items_center()
          .child(v_flex().flex_1().min_w(0).gap(4)
            .child(div().font_semibold().text_size(19).child("Beads"))
            .child(div().truncate().text_size(12).text_color(t.muted).tooltip(this.project?.folder ?? this.folder ?? "").child(`${name}${this.machine ? ` · ${this.machine}` : ""}`)))
          .child(this.button("refresh-beads", this.loading ? "Refreshing…" : "Refresh", (cx) => this.refresh(cx), t)))
        .child(h_flex().gap(8).items_center()
          .child(this.button("filter-all", `All open · ${counts.all}`, (cx) => this.setMode("all", cx), t, this.mode === "all"))
          .child(this.button("filter-ready", `Ready · ${counts.ready}`, (cx) => this.setMode("ready", cx), t, this.mode === "ready")))
        .child(div().text_size(11).text_color(t.muted).child(this.loaded ? `${count} ${count === 1 ? "issue" : "issues"} · ${sessions.size} with live ${sessions.size === 1 ? "session" : "sessions"}` : "Loading project…")))
      .when(!!this.error, (element) => element.child(div().flex_none().px(16).py(10).bg(t.surface).text_color(t.blocked).child(`${this.error}${this.loaded ? " — showing last successful refresh" : ""}`)))
      .when(!!this.notice, (element) => element.child(div().flex_none().px(16).py(8).text_size(12).text_color(t.done).child(this.notice)))
      .child(v_flex().id("beads-viewport").flex_1().min_h(0).relative().overflow_hidden()
        .on_size_change((size, cx) => {
          if (this.viewportHeight !== size.height) { this.viewportHeight = size.height; cx.notify(); }
        })
        .when(rows.length > 0, (element) => element
          .child(v_virtual_list("beads-list", rows.length, 68, (index) => rows[index].issue.id,
            (range) => rows.slice(range.start, range.end).map((row) => {
              const entity = this.rowEntities.get(row.issue.id);
              return div().h(68).when(!!entity, (element) => element.child(entity));
            }))
            .w_full().h(this.viewportHeight ?? 0).track_scroll(this.scroll))
          .child(Scrollbar.vertical("beads-list").absolute().inset_0()))
        .when(!rows.length, (element) => element.child(v_flex().p(24).gap(8)
          .child(div().text_color(t.muted).child(!this.loaded ? this.loading ? "Loading beads…" : "Could not load this project." : this.mode === "ready" ? "No ready or in-progress issues." : "No open issues.")))))
      .child(h_flex().flex_none().px(16).py(8).gap(12).border_t(1).border_color(t.border).text_size(10.5).text_color(t.dim)
        .child(div().flex_1().child("Session status ≠ issue status · Done is an unseen finished turn"))
        .when(!!this.updated, (element) => element.child(div().child(`Updated ${this.updated}`))));
  }
}

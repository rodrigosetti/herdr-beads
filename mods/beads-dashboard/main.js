import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { agents, exec, focused, nextCommand, panes, props, run, setShared, spaces, terminals, theme } from "herdr";
import { issueRows, issueSessions, sessionLabels, statusLabels } from "./model.js";

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
    this.blocked = new Set();
    this.folded = new Set();
    this.hideBlocked = false;
    this.query = "";
    this.error = "";
    this.notice = "";
    this.loading = false;
    this.loaded = false;
    this.starting = new Set();
    this.handle = cx.focus_handle();
    this.input = InputState.new({ value: "", placeholder: "Find by title, ID, type or label" });
    this.input.on("change", (_event, cx) => { this.query = this.input.value(); cx.notify(); });
    this.refresh(cx);
    cx.timer.every(10000, (cx) => this.refresh(cx));
    cx.spawn(async (cx) => {
      for (;;) {
        const command = await nextCommand();
        if (command === "focus") this.handle.focus();
        else if (command === "refresh") this.refresh(cx);
        else if (command === "toggle-blocked") this.toggleBlocked(cx);
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
          this.hideBlocked = !!saved.hideBlocked;
          this.folded = new Set(saved.folded ?? []);
        }
        const [issues, blocked] = await Promise.all([
          this.bd(["list", "--limit", "0", "--sort", "priority", "--brief"]),
          this.bd(["blocked"]),
        ]);
        const byId = new Map(issues.map((issue) => [issue.id, issue]));
        const fetched = new Set();
        for (;;) {
          const missing = [...new Set([...byId.values()].map((issue) => issue.parent).filter((id) => id && !byId.has(id) && !fetched.has(id)))];
          if (!missing.length) break;
          missing.forEach((id) => fetched.add(id));
          const parents = await this.bd(["show", ...missing, "--brief-deps"]);
          for (const parent of parents) byId.set(parent.id, { ...parent, context: true });
        }
        this.issues = [...byId.values()];
        this.blocked = new Set(blocked.map((issue) => issue.id));
        for (const issue of issues) if (issue.status === "blocked") this.blocked.add(issue.id);
        this.loaded = true;
        this.updated = new Date().toLocaleTimeString();
        this.error = "";
      } catch (error) {
        this.error = String(error.message ?? error);
      } finally {
        this.loading = false;
        cx.notify();
      }
    });
  }

  save() {
    if (this.prefKey) localStorage.setItem(this.prefKey, JSON.stringify({ hideBlocked: this.hideBlocked, folded: [...this.folded] }));
  }

  toggleBlocked(cx) {
    this.hideBlocked = !this.hideBlocked;
    this.save();
    cx.notify();
  }

  ownPane() {
    const all = panes();
    return all.find((pane) => pane.mod === "beads-dashboard" && pane.tab === this.tab)
      ?? all.find((pane) => pane.mod === "beads-dashboard" && pane.focused);
  }

  select(id, cx) {
    if (!this.project || !this.issues.some((issue) => issue.id === id)) return;
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
    if (this.blocked.has(id) || !["open", "in_progress"].includes(issue.status)) {
      this.error = `${id} is not available to start. Resolve its blockers or deferred status first.`;
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
        this.notice = `${result.action === "focused" ? "Opened existing session" : "Started session"} for ${id}`;
        this.refresh(cx);
      } catch (error) { this.error = String(error.message ?? error); }
      finally { this.starting.delete(id); cx.notify(); }
    });
  }

  button(id, label, action, t, active = false) {
    return div().id(id).flex_none().px(10).py(6).rounded(6).border(1).border_color(active ? t.accent : t.border)
      .bg(active ? t.selected : t.surface).text_color(active ? t.foreground : t.muted).text_size(12).cursor_pointer()
      .hover((style) => style.bg(t.hover).text_color(t.foreground)).on_click((_event, cx) => { cx.stop_propagation(); action(cx); }).child(label);
  }

  row(row, sessions, t) {
    const { issue, depth, context, children, folded } = row;
    const live = sessions.get(issue.id) ?? [], session = live[0];
    const blocked = this.blocked.has(issue.id), busy = this.starting.has(issue.id);
    const canStart = !context && !blocked && ["open", "in_progress"].includes(issue.status);
    const priorityColor = [t.blocked, t.ansi3, t.ansi6, t.muted, t.dim][issue.priority] ?? t.dim;
    const fold = div().id(`fold-${issue.id}`).w(16).flex_none().text_color(t.dim)
      .when(children > 0, (element) => element.cursor_pointer().on_click((_event, cx) => {
        cx.stop_propagation();
        this.folded.has(issue.id) ? this.folded.delete(issue.id) : this.folded.add(issue.id);
        this.save(); cx.notify();
      })).child(children ? folded ? "▸" : "▾" : "");
    const title = div().flex_1().min_w(0).truncate().text_size(13).text_color(context ? t.muted : t.foreground)
      .when(issue.issue_type === "epic", (element) => element.font_semibold()).tooltip(issue.title).child(issue.title);
    const info = h_flex().gap(9).items_center().text_size(11).text_color(t.dim)
      .child(div().flex_none().child(issue.id))
      .child(div().flex_none().text_color(t.muted).child(issue.issue_type))
      .child(div().flex_none().text_color(issue.status === "in_progress" ? t.working : t.muted).child(statusLabels[issue.status] ?? issue.status))
      .when(blocked, (element) => element.child(div().text_color(t.blocked).child("Dependency blocked")))
      .when(context, (element) => element.child(div().child("Group context")));
    const action = session ? this.button(`go-${issue.id}`, "Go to session", () => this.focusSession(session), t)
      : busy ? div().flex_none().text_size(12).text_color(t.muted).frames(["Starting", "Starting ·", "Starting ··"], 300)
      : canStart ? this.button(`start-${issue.id}`, "Start", (cx) => this.launch(issue.id, cx), t)
      : div().flex_none().w(58);
    return h_flex().id(`bead-${issue.id}`).flex_none().min_h(68).pl(10 + Math.min(depth, 8) * 18).pr(12).py(10).gap(8).items_center()
      .border_b(1).border_color(t.border).cursor_pointer().bg(this.selected === issue.id ? t.selected : issue.issue_type === "epic" ? t.surface : t.background)
      .hover((style) => style.bg(t.hover)).on_click((_event, cx) => this.select(issue.id, cx))
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
    const t = theme(), sessions = this.sessionMap();
    const { rows, count } = issueRows(this.issues, this.blocked, { hideBlocked: this.hideBlocked, query: this.query, folded: this.folded });
    const total = this.issues.filter((issue) => !issue.context && issue.status !== "closed").length;
    const name = this.project?.folder.replace(/[\\/]$/, "").split(/[\\/]/).pop() || "Project";
    return v_flex().id("beads-dashboard").size_full().min_h(0).bg(t.background).text_color(t.foreground).text_size(13)
      .track_focus(this.handle).on_mouse_down("left", () => this.handle.focus())
      .child(v_flex().flex_none().p(16).gap(12).border_b(1).border_color(t.border)
        .child(h_flex().gap(10).items_center()
          .child(v_flex().flex_1().min_w(0).gap(4)
            .child(div().font_semibold().text_size(19).child("Beads"))
            .child(div().truncate().text_size(12).text_color(t.muted).tooltip(this.project?.folder ?? this.folder ?? "").child(`${name}${this.machine ? ` · ${this.machine}` : ""}`)))
          .child(this.button("refresh-beads", this.loading ? "Refreshing…" : "Refresh", (cx) => this.refresh(cx), t)))
        .child(Input.new(this.input).h(32).px(10).items_center().rounded(6).border(1).border_color(t.input).bg(t.surface).text_color(t.foreground))
        .child(h_flex().gap(10).items_center()
          .child(this.button("hide-blocked", this.hideBlocked ? "Blocked hidden" : "Show all · including blocked", (cx) => this.toggleBlocked(cx), t, this.hideBlocked))
          .child(div().flex_1().text_size(11).text_color(t.muted).child(this.loaded ? `${count} of ${total} issues · ${sessions.size} with live ${sessions.size === 1 ? "session" : "sessions"}` : "Loading project…"))))
      .when(!!this.error, (element) => element.child(div().flex_none().px(16).py(10).bg(t.surface).text_color(t.blocked).child(`${this.error}${this.loaded ? " — showing last successful refresh" : ""}`)))
      .when(!!this.notice, (element) => element.child(div().flex_none().px(16).py(8).text_size(12).text_color(t.done).child(this.notice)))
      .child(v_flex().id("beads-list").flex_1().min_h(0).overflow_y_scrollbar().children(rows.map((row) => this.row(row, sessions, t)))
        .when(!rows.length, (element) => element.child(v_flex().p(24).gap(8)
          .child(div().text_color(t.muted).child(!this.loaded ? this.loading ? "Loading beads…" : "Could not load this project." : "No issues match these filters."))
          .when(this.hideBlocked && this.loaded, (item) => item.child(div().text_size(12).text_color(t.dim).child("Show blocked issues to see work waiting on dependencies."))))))
      .child(h_flex().flex_none().px(16).py(8).gap(12).border_t(1).border_color(t.border).text_size(10.5).text_color(t.dim)
        .child(div().flex_1().child("Session status ≠ issue status · Done is an unseen finished turn"))
        .when(!!this.updated, (element) => element.child(div().child(`Updated ${this.updated}`))));
  }
}

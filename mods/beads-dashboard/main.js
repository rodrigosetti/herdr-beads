import { View, div } from "gpui-kit";
import { v_flex, h_flex, v_virtual_list, VirtualListScrollHandle, Scrollbar, TextView, EditorState, Editor } from "gpui-base";
import { agents, exec, focused, home, nextCommand, panes, props, run, setShared, shared, spaces, terminals, theme } from "herdr";
import { issueAction, issueRows, issueSessions, sessionLabels, statusLabels } from "./model.js";
import { lifecycleLabel, targetLabel, lifecycleMarkdown, manualClosureCommand, retainedRuns } from "./lifecycle.js";

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
    this.modName = target.dashboardMod || "beads-dashboard";
    this.project = null;
    this.bridge = target.lifecycleBridge || `${home()}/.local/share/herdr-beads-service/bin/beads_ui.py`;
    this.python = target.lifecyclePython || `${home()}/.local/share/herdr-beads-service/venv/bin/python`;
    this.serviceConfig = target.lifecycleConfig;
    this.service = { available: false, error: "Connecting to local lifecycle service…" };
    this.lifecycle = new Map();
    this.pendingRequests = {};
    this.lifecycleOpen = false;
    this.lifecycleId = null;
    this.editor = null;
    this.editorKind = null;
    this.target = null;
    this.issues = [];
    this.epics = [];
    this.blocked = new Set();
    this.ready = new Set();
    this.folded = new Set();
    this.mode = "all";
    this.error = "";
    this.operationError = "";
    this.reconciliationError = "";
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
    // Native timer callbacks enter the dashboard's ownership scope. A promise
    // woken by a row keeps that row's scope even after cx.sleep().
    // Idle ticks do not notify or redraw; render only queues reconciliation.
    cx.timer.every(50, (cx) => {
      while (this.actions.length) this.actions.shift()(cx);
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
        else if (command.startsWith("lifecycle:")) this.inspectLifecycle(command.slice(10), cx);
        else if (command === "lifecycle") { this.lifecycleOpen = !this.lifecycleOpen; }
        else if (command === "configure") this.editTarget(cx);
        else if (command === "operation:handoff") this.prepareHandoff(cx);
        else if (command === "operation:submit") this.submitEditor(cx);
        else if (command === "copy-closure") this.copyClosure(this.lifecycleId, cx);
        else if (command.startsWith("operation:")) this.operate(this.lifecycleId, command.slice(10), cx);
        cx.notify();
      }
    });
  }

  async bd(args) {
    return checked(await exec("env", [`BEADS_DIR=${this.project.beads}`, "bd", ...args, "--json"], { cwd: this.project.folder, on: this.machine }), "bd");
  }

  async controller(args) {
    if (this.machine) throw new Error("remote_machine: Lifecycle actions are local-machine only. Open this project on its recorded machine; no local fallback is allowed.");
    const result = await exec(this.python, [this.bridge, ...args, ...(this.serviceConfig ? ["--config", this.serviceConfig] : [])], { timeout: 25000 });
    let response;
    try { response = JSON.parse(result.stdout); }
    catch { throw new Error((result.stderr || result.stdout || "Lifecycle service unavailable. Install/start the controller; no launcher fallback.").trim()); }
    if (!response || typeof response !== "object") throw new Error("Invalid controller response.");
    return response;
  }

  scope(id) { return { ...this.project.scope, issueId: id }; }

  async request(id, method, params, retained = null) {
    const request = retained ?? { apiVersion: 1, requestId: `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`, scope: this.scope(id), method, params };
    const selectedScope = this.scope(id), requestedScope = request.scope;
    if (requestedScope.issueId !== id || requestedScope.machineId !== selectedScope.machineId || requestedScope.beadsDir !== selectedScope.beadsDir ||
        requestedScope.repository.root !== selectedScope.repository.root || requestedScope.repository.commonDir !== selectedScope.repository.commonDir)
      throw new Error("Retained request belongs to another project or machine; it cannot be replayed here.");
    if (method !== "status") {
      this.pendingRequests[id] = request;
      this.save();
    }
    const response = await this.controller(["request", "--json", JSON.stringify(request)]);
    if (response.run) {
      const scope = response.run.scope;
      if (scope.issueId !== id || scope.machineId !== request.scope.machineId || scope.beadsDir !== request.scope.beadsDir ||
          scope.repository.root !== request.scope.repository.root || scope.repository.commonDir !== request.scope.repository.commonDir)
        throw new Error("Controller returned a different project/machine/run scope.");
    }
    if (response.outcome === "ok" || response.outcome === "accepted") {
      const previous = this.lifecycle.get(id);
      this.lifecycle.set(id, { ...previous, ...response, actions: response.actions ?? previous?.actions ?? [] });
      if (method !== "status") delete this.pendingRequests[id];
      if (response.operation?.requestId === this.pendingRequests[id]?.requestId) delete this.pendingRequests[id];
      this.save();
    } else {
      if (response.run) this.lifecycle.set(id, { ...this.lifecycle.get(id), ...response, actions: [] });
      if (method !== "status" && response.outcome === "refused") delete this.pendingRequests[id];
      this.save();
      throw new Error(`${response.error?.code ?? response.outcome}: ${response.error?.message ?? "Controller refused the operation."}${response.operation ? ` · Operation ${response.operation.operationId}` : ""}`);
    }
    return response;
  }

  async refreshLifecycle() {
    if (this.machine) {
      this.service = { available: false, error: "remote_machine: Lifecycle actions require the recorded local machine. No local fallback." };
      return;
    }
    try {
      const snapshot = await this.controller(["project", "--folder", this.folder]);
      if (!snapshot.project) throw new Error(snapshot.error?.message ?? "Controller project discovery failed.");
      if (this.project && (this.project.beads !== snapshot.project.beads || this.project.folder !== snapshot.project.folder))
        throw new Error("Canonical project changed; reopen the dashboard rather than mix run identities.");
      this.project = snapshot.project;
      if (!this.prefKey) {
        this.prefKey = `beads.dashboard.${JSON.stringify([this.machine, this.project.beads, this.project.folder])}`;
        const saved = preferences(this.prefKey);
        this.mode = saved.mode === "ready" ? "ready" : "all";
        this.folded = new Set(saved.folded ?? []);
        this.pendingRequests = saved.pendingRequests ?? {};
        this.operationError = saved.operationError ?? "";
        this.notice = saved.notice ?? "";
        this.target = saved.target ?? null;
        for (const response of saved.lifecycle ?? []) {
          const scope = response.run?.scope;
          if (scope && scope.beadsDir === this.project.beads && scope.machineId === this.project.scope.machineId &&
              scope.repository.root === this.project.folder && scope.repository.commonDir === this.project.scope.repository.commonDir)
            this.lifecycle.set(scope.issueId, response);
        }
      }
      this.service = snapshot.service;
      if (!this.target) this.target = snapshot.target;
      if (snapshot.service.available) {
        this.lifecycle = new Map(snapshot.runs.filter((response) => response.run).map((response) => [response.run.scope.issueId, response]));
        this.epics = snapshot.epics;
        this.reconciliationError = (snapshot.errors ?? []).map((error) => error.message ?? error.error ?? String(error)).join("\n");
        if (this.lifecycleId) await this.request(this.lifecycleId, "status", { runId: null });
        this.save();
      }
    } catch (error) {
      this.service = { available: false, error: String(error.message ?? error) };
    }
  }

  refresh(cx) {
    if (this.loading) return;
    this.loading = true;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        if (!this.project) {
          if (!this.folder) throw new Error("No project folder. Open this dashboard from a project space.");
          await this.refreshLifecycle();
          if (!this.project) {
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
        } else await this.refreshLifecycle();
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
        if (this.selected && !byId.has(this.selected) && !this.navigationIssues.some((issue) => issue.id === this.selected)) this.observedSelection = null;
        this.blocked = new Set(blocked.map((issue) => issue.id));
        for (const issue of issues) if (issue.status === "blocked") this.blocked.add(issue.id);
        this.ready = new Set(ready.map((issue) => issue.id));
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
    if (this.prefKey) localStorage.setItem(this.prefKey, JSON.stringify({
      mode: this.mode, folded: [...this.folded], lifecycle: [...this.lifecycle.values()], pendingRequests: this.pendingRequests, target: this.target,
      operationError: this.operationError, notice: this.notice,
    }));
  }

  setMode(mode, cx) {
    this.mode = mode;
    this.lifecycleOpen = false;
    this.save();
    cx.notify();
  }

  ownPane() {
    const all = panes();
    return all.find((pane) => pane.mod === this.modName && pane.focused)
      ?? all.find((pane) => pane.mod === this.modName && pane.tab === this.tab);
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
  }

  synchronizeRows(rows, sessions) {
    const snapshot = JSON.stringify([rows, [...sessions], [...this.ready], [...this.blocked], [...this.starting], this.selected, [...this.lifecycle], this.service.available]);
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
    this.editor?.release();
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


  inspectLifecycle(id, cx) {
    this.lifecycleOpen = true;
    this.lifecycleId = id;
    this.editorKind = null;
    this.editor?.release();
    this.editor = null;
    if (!this.project?.scope || !this.service.available) { cx.notify(); return; }
    cx.spawn(async (cx) => {
      try { await this.request(id, "status", { runId: null }); }
      catch (error) { this.operationError = String(error.message ?? error); this.save(); }
      cx.notify();
    });
  }

  launch(id, cx) {
    this.operate(id, "start_or_focus", cx);
  }

  operate(id, method, cx) {
    if (!id || !this.project?.scope || this.starting.has(id)) return;
    if (!this.service.available || this.machine) {
      this.operationError = this.service.error?.message ?? this.service.error ?? "Lifecycle service unavailable; no launcher fallback.";
      cx.notify(); return;
    }
    if (this.pendingRequests[id] && method !== "recover") {
      this.operationError = `Request ${this.pendingRequests[id].requestId} is uncertain. Inspect status or recover that same request, not a new operation.`;
      cx.notify(); return;
    }
    this.starting.add(id);
    this.operationError = "";
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        if (method === "recover") {
          const retained = this.pendingRequests[id];
          if (!retained) return;
          const result = await this.request(id, retained.method, retained.params, retained);
          this.notice = `${id}: recovered request ${retained.requestId}${result.operation ? ` · Operation ${result.operation.operationId} · ${result.operation.state}` : ""}`;
          await this.refreshLifecycle();
          return;
        }
        const reviewed = this.lifecycle.get(id)?.run;
        const response = await this.request(id, "status", { runId: null }), current = response.run;
        if (reviewed && (reviewed.fence.runId !== current?.fence.runId || reviewed.fence.attempt !== current.fence.attempt || reviewed.fence.generation !== current.fence.generation))
          throw new Error("stale_attempt: Run identity changed. Review its current lifecycle before acting.");
        const action = response.actions?.find((action) => action.method === method);
        if (!action?.enabled) throw new Error(`${method} unavailable: ${action?.refusal ?? "controller did not advertise this operation"}`);
        const expected = current ? { fence: current.fence, revision: current.revision } : null;
        let params;
        if (method === "start_or_focus") params = { expected, harness: "omp", target: current ? current.target : this.target,
          policy: current?.policy ?? { closeAfterIntegration: false, cleanupAfterRelease: false }, newAttempt: false };
        else if (method === "resume") params = { expected, access: current.execution.access };
        else if (method === "release") params = { expected, reason: "Owner explicitly requested release in Herdr; retain all resources for manual cleanup." };
        else params = { expected };
        const result = await this.request(id, method, params);
        this.notice = `${id}: ${result.action ?? result.outcome}${result.operation ? ` · Operation ${result.operation.operationId} · ${result.operation.state}` : ""}`;
        await this.refreshLifecycle();
      } catch (error) {
        const pending = this.pendingRequests[id];
        this.operationError = `${String(error.message ?? error)}${pending ? ` · Request ${pending.requestId} retained. Refresh status before any new action; do not duplicate an uncertain start.` : ""}`;
      } finally { this.starting.delete(id); this.save(); cx.notify(); }
    });
  }

  editTarget(cx) {
    this.editor?.release();
    this.editorKind = "target";
    this.editor = EditorState.new({ language: "json", value: JSON.stringify(this.target, null, 2) });
    cx.notify();
  }

  prepareHandoff(cx) {
    const id = this.lifecycleId, response = this.lifecycle.get(id), current = response?.run;
    if (!current || !response.actions?.some((action) => action.method === "handoff" && action.enabled) || !this.service.available) return;
    cx.spawn(async (cx) => {
      try {
        const head = await exec("git", ["rev-parse", "HEAD"], { cwd: current.workspace.path });
        if (head.code !== 0) throw new Error(head.stderr || "Cannot read workspace HEAD.");
        if (id !== this.lifecycleId) return;
        const sha = head.stdout.trim(), target = current.target;
        const requiredSubmissions = current.submissions
          .filter(({ identity }) => current.coverage.required.some((key) => key.submissionId === identity.submissionId && key.submissionRevision === identity.submissionRevision))
          .map(({ identity }) => identity);
        if (!requiredSubmissions.some((identity) => identity.head === sha)) requiredSubmissions.push({
          submissionId: `review-${Date.now()}`, submissionRevision: 1, head: sha, target,
          pullRequest: target?.mode === "github-pr" ? { number: 0, url: "" } : null,
        });
        this.editor?.release();
        this.editorKind = "handoff";
        this.editorExpected = { fence: current.fence, revision: current.revision };
        this.editor = EditorState.new({ language: "json", value: JSON.stringify({
          previousCoverageRevision: current.coverage.revision, target, workspaceHead: sha,
          requiredSubmissions,
          summary: "Ready for owner integration review", nextSteps: ["Owner integrates into the explicit target"], risks: [],
        }, null, 2) });
      } catch (error) { this.operationError = String(error.message ?? error); this.save(); }
      cx.notify();
    });
  }

  submitEditor(cx) {
    if (!this.editor) return;
    try {
      const value = JSON.parse(this.editor.value());
      if (this.editorKind === "target") {
        this.target = value;
        this.editor.release(); this.editor = null; this.editorKind = null;
        this.save();
        this.notice = "Destination selected for new runs. Existing runs retain their recorded target; change it through an explicit handoff.";
      } else {
        const id = this.lifecycleId, expected = this.editorExpected, editor = this.editor;
        if (!this.service.available || this.starting.has(id) || this.pendingRequests[id]) throw new Error("Operation unavailable or uncertain; refresh/recover status before submitting.");
        this.starting.add(id);
        this.operationError = "";
        cx.spawn(async (cx) => {
          try {
            await this.request(id, "handoff", { ...value, expected });
            if (this.editor === editor) { editor.release(); this.editor = null; this.editorKind = null; }
            await this.refreshLifecycle();
          } catch (error) { this.operationError = String(error.message ?? error); }
          finally { this.starting.delete(id); this.save(); }
          cx.notify();
        });
      }
    } catch (error) { this.operationError = String(error.message ?? error); this.save(); }
    cx.notify();
  }

  copyClosure(id, cx, reviewed = null) {
    const command = this.service.available ? manualClosureCommand(this.lifecycle.get(id)) : null;
    if (command && (!reviewed || reviewed === command)) {
      cx.write_to_clipboard(command);
      this.notice = `${id}: Copied only. Normal manual Beads closure is not revision/run guarded; review current ownership and evidence before executing.`;
    } else this.operationError = "No current verified manual-closure handoff. Refresh and review the current run; no closure command was copied or executed.";
    this.save();
    cx.notify();
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
    const response = this.lifecycle.get(issue.id);
    const decision = issueAction(issue, { response, blocked, context, available: this.service.available });
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
      .when(context, (element) => element.child(div().child(this.selected === issue.id ? "Navigation context" : "Group context")))
      .when(!!response?.run, (element) => element.child(div().truncate().text_color(t.muted).child(lifecycleLabel(response))));
    const action = busy ? div().flex_none().text_size(12).text_color(t.muted).child("Operation pending")
      : decision ? this.button(`start-${issue.id}`, decision === "focus" ? "Go to session" : decision === "resume" ? "Resume" : "Start",
        (cx) => this.operate(issue.id, decision === "resume" ? "resume" : "start_or_focus", cx), t)
      : div().flex_none();
    return h_flex().id(`bead-${issue.id}`).flex_none().h(68).pl(10 + Math.min(depth, 8) * 18).pr(12).py(10).gap(8).items_center()
      .border_b(1).border_color(t.border).cursor_pointer().bg(this.selected === issue.id ? t.selected : issue.issue_type === "epic" ? t.surface : t.background)
      .hover((style) => style.bg(t.hover)).on_click((_event, cx) => {
        cx.stop_propagation();
        this.dispatch((cx) => this.select(issue.id, cx));
      })
      .child(fold)
      .child(div().w(27).flex_none().text_size(11).font_semibold().text_color(priorityColor).child(`P${issue.priority ?? "?"}`))
      .child(v_flex().flex_1().min_w(0).gap(5).child(title).child(info))
      .when(!!session, (element) => element.child(div().id(`session-${issue.id}`).flex_none().text_size(11)
        .text_color(t[session.status] ?? t.muted).tooltip(live.map((item) => `${item.address}: ${sessionLabels[item.status] ?? item.status}`).join("\n"))
        .tooltip("Herdr attention only; does not grant lifecycle execution or prove integration.")
        .child(`${sessionLabels[session.status] ?? session.status}${live.length > 1 ? ` +${live.length - 1}` : ""}`)))
      .child(action)
      .when(issue.issue_type !== "epic" || !!response?.run, (element) => element.child(
        this.button(`lifecycle-${issue.id}`, "Actions", (cx) => this.inspectLifecycle(issue.id, cx), t)));
  }

  lifecyclePanel(t) {
    const responses = retainedRuns([...this.lifecycle.values()]);
    const response = this.lifecycle.get(this.lifecycleId);
    const current = response?.run;
    const actions = response?.actions ?? [];
    const enabled = (method) => this.service.available && !this.starting.has(this.lifecycleId) && actions.some((action) => action.method === method && action.enabled);
    const command = this.service.available ? manualClosureCommand(response) : null;
    return v_flex().id("lifecycle-panel").flex_1().min_h(0).overflow_y_scrollbar().p(16).gap(10)
      .child(div().font_semibold().child("Run lifecycle · retained workspaces"))
      .child(div().text_size(11).text_color(t.muted).child("Independent of Working / Needs you / Done unseen / Idle. No automatic closure or resource deletion."))
      .children(responses.map((item) => this.button(`retained-${item.run.fence.runId}`,
        `${item.run.scope.issueId} · ${lifecycleLabel(item)} · ${item.run.cleanup.blockers.join(", ")}`,
        (cx) => this.inspectLifecycle(item.run.scope.issueId, cx), t, item.run.scope.issueId === this.lifecycleId)))
      .when(!responses.length && !this.lifecycleId, (element) => element.child(div().text_color(t.muted).child("No retained runs in this project. Open an issue's Actions to configure and start omp.")))
      .when(!!this.lifecycleId, (element) => element
        .child(div().font_semibold().child(`${this.lifecycleId} · ${lifecycleLabel(response)}`))
        .when(!!this.pendingRequests[this.lifecycleId], (element) => element.child(div().text_color(t.blocked)
          .child(`Uncertain request ${this.pendingRequests[this.lifecycleId].requestId}. Refresh status; do not repeat an uncertain launch.`)))
        .child(div().text_size(12).text_color(t.muted).child(targetLabel(current ? current.target : this.target)))
        .when(current?.closure.state === "held" && !!current.closure.reason, (element) => element.child(div().text_size(12).text_color(t.blocked)
          .child(`${current.target?.mode ?? "Unbound destination"} blocker: ${current.closure.reason}`)))
        .child(h_flex().flex_wrap().gap(8)
          .when(!!this.pendingRequests[this.lifecycleId] && this.service.available, (element) => element.child(this.button("recover-request", "Recover same request", (cx) => this.operate(this.lifecycleId, "recover", cx), t)))
          .when(enabled("start_or_focus"), (element) => element.child(this.button("lifecycle-start", current ? "Focus / resume omp" : "Start omp", (cx) => this.launch(this.lifecycleId, cx), t)))
          .when(enabled("resume"), (element) => element.child(this.button("lifecycle-resume", "Exact resume", (cx) => this.operate(this.lifecycleId, "resume", cx), t)))
          .when(enabled("handoff"), (element) => element.child(this.button("lifecycle-handoff", current?.target?.mode === "github-pr" ? "Submit PR handoff" : "Submit explicit handoff", (cx) => this.prepareHandoff(cx), t)))
          .when(enabled("reconcile"), (element) => element.child(this.button("lifecycle-reconcile", "Verify integration", (cx) => this.operate(this.lifecycleId, "reconcile", cx), t))))
        .children(actions.filter((action) => !action.enabled).map((action) => div().text_size(11).text_color(t.muted).child(`${action.method} unavailable: ${action.refusal}`)))
        .when(enabled("release"), (element) => element.child(this.button("lifecycle-release", "Release ownership — retain workspace", (cx) => this.operate(this.lifecycleId, "release", cx), t)))
        .when(!current && !this.editor, (element) => element.child(this.button("edit-target", "Configure explicit destination", (cx) => this.editTarget(cx), t)))
        .when(!!command, (element) => element
          .child(div().text_size(12).text_color(t.muted).child("Copy only. Normal bd close is not revision/run guarded; review current owner, run, target and evidence before executing outside Herdr."))
          .child(this.button("copy-manual-close", "Copy normal bd close command — review only", (cx) => this.copyClosure(current.scope.issueId, cx, command), t)))
        .when(!!response && !this.editor, (element) => element.child(TextView.markdown(`lifecycle-evidence-${this.lifecycleId}`, lifecycleMarkdown(response, { available: this.service.available })).selectable(true).scrollable(false))))
      .when(!!this.editor, (element) => element
        .child(div().text_size(12).text_color(t.muted).child(this.editorKind === "target"
          ? "Explicit Target JSON. Local mode uses only canonical Git/ref; GitHub mode requires stable provider repository identity and base."
          : "Review the complete required submission set. PR mode requires the genuine PR number/URL. This records a handoff only; it never merges or pushes."))
        .child(div().h(300).child(Editor.new(this.editor)))
        .child(h_flex().gap(8)
          .child(this.button("submit-lifecycle-editor", this.editorKind === "target" ? "Use destination" : "Record submission", (cx) => this.submitEditor(cx), t))
          .child(this.button("cancel-lifecycle-editor", "Cancel", (cx) => { this.editor.release(); this.editor = null; this.editorKind = null; cx.notify(); }, t))));
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
      .track_focus(this.handle).on_click(() => this.handle.focus())
      .child(v_flex().flex_none().p(16).gap(12).border_b(1).border_color(t.border)
        .child(h_flex().gap(10).items_center()
          .child(v_flex().flex_1().min_w(0).gap(4)
            .child(div().font_semibold().text_size(19).child("Beads"))
            .child(div().truncate().text_size(12).text_color(t.muted).tooltip(this.project?.folder ?? this.folder ?? "").child(`${name}${this.machine ? ` · ${this.machine}` : ""}`)))
          .child(this.button("refresh-beads", this.loading ? "Refreshing…" : "Refresh", (cx) => this.refresh(cx), t)))
        .child(h_flex().gap(8).items_center()
          .child(this.button("filter-all", `All open · ${counts.all}`, (cx) => this.setMode("all", cx), t, this.mode === "all"))
          .child(this.button("filter-ready", `Ready · ${counts.ready}`, (cx) => this.setMode("ready", cx), t, this.mode === "ready" && !this.lifecycleOpen))
          .child(this.button("filter-lifecycle", `Lifecycle · ${retainedRuns([...this.lifecycle.values()]).length}`, (cx) => { this.lifecycleOpen = !this.lifecycleOpen; cx.notify(); }, t, this.lifecycleOpen)))
        .child(div().text_size(11).text_color(t.muted).child("Supported execution: omp only · Herdr agent compatibility does not imply lifecycle support."))
        .child(div().text_size(11).text_color(t.muted).child(this.loaded ? `${count} ${count === 1 ? "issue" : "issues"} · ${sessions.size} with agent attention` : "Loading project…")))
      .when(!!this.error, (element) => element.child(div().flex_none().px(16).py(10).bg(t.surface).text_color(t.blocked).child(`${this.error}${this.loaded ? " — showing last successful refresh" : ""}`)))
      .when(!!this.reconciliationError, (element) => element.child(div().flex_none().px(16).py(10).bg(t.surface).text_color(t.blocked).child(`Epic reconciliation failed: ${this.reconciliationError}`)))
      .when(!this.service.available, (element) => element.child(div().flex_none().px(16).py(10).bg(t.surface).text_color(t.blocked)
        .child(`${this.service.error?.message ?? this.service.error ?? "Lifecycle service unavailable."} — lifecycle snapshot is last-good/read-only; browsing remains available. No local-launch fallback.`)))
      .when(!!this.operationError, (element) => element.child(div().flex_none().px(16).py(10).bg(t.surface).text_color(t.blocked).child(`Lifecycle operation: ${this.operationError}`)))
      .when(!!this.notice, (element) => element.child(div().flex_none().px(16).py(8).text_size(12).text_color(t.done).child(this.notice)))
      .when(this.lifecycleOpen, (element) => element.child(this.lifecyclePanel(t)))
      .when(!this.lifecycleOpen, (element) => element.child(v_flex().id("beads-viewport").flex_1().min_h(0).relative().overflow_hidden()
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
          .child(div().text_color(t.muted).child(!this.loaded ? this.loading ? "Loading beads…" : "Could not load this project." : this.mode === "ready" ? "No ready or in-progress issues." : "No open issues."))))))
      .child(h_flex().flex_none().px(16).py(8).gap(12).border_t(1).border_color(t.border).text_size(10.5).text_color(t.dim)
        .child(div().flex_1().child("Session status ≠ issue status · Done is an unseen finished turn"))
        .when(!!this.updated, (element) => element.child(div().child(`Updated ${this.updated}`))));
  }
}

import { View, div } from "gpui-kit";
import { h_flex, v_flex, TextView } from "gpui-base";
import { exec, nextCommand, props, shared, theme } from "herdr";

const NARRATIVE = new Set(["description", "design", "acceptance_criteria", "notes", "body", "text", "comment"]);
const SUMMARY = new Set(["id", "title", "status", "issue_type", "priority"]);
const LABELS = { issue_type: "Type", acceptance_criteria: "Acceptance criteria", dependency_type: "Dependency type" };
const label = (key) => LABELS[key] ?? key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const nonempty = (value) => value !== undefined && value !== null && value !== "" &&
  (!Array.isArray(value) || value.length > 0) &&
  (typeof value !== "object" || Array.isArray(value) || Object.keys(value).length > 0);
const escape = (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/[\\`*_{}\[\]()#+.!|~-]/g, "\\$&").replace(/\n/g, "  \n");

function jsonBlock(value) {
  const text = JSON.stringify(value, null, 2);
  const ticks = text.match(/`+/g) ?? [];
  const fence = "`".repeat(Math.max(3, ...ticks.map((s) => s.length + 1)));
  return `${fence}json\n${text}\n${fence}`;
}

function field(key, value, depth = 2) {
  if (!nonempty(value)) return "";
  const heading = `${"#".repeat(depth)} ${escape(label(key))}`;
  if (typeof value === "string") return `${heading}\n\n${NARRATIVE.has(key) ? value : escape(value)}`;
  if (typeof value !== "object") return `${heading}\n\n${escape(value)}`;
  if (Array.isArray(value) && value.every((item) => item && typeof item === "object" && !Array.isArray(item))) {
    return `${heading}\n\n${value.map((item, index) => {
      const title = item.title ?? item.author ?? item.id ?? `Entry ${index + 1}`;
      // Keep every field, including identity and relationship metadata, below the entry title.
      const fields = Object.entries(item).filter(([, v]) => nonempty(v));
      return `${"#".repeat(Math.min(depth + 1, 6))} ${escape(title)}\n\n${fields.map(([k, v]) => {
        if (typeof v === "string" && NARRATIVE.has(k)) return `**${escape(label(k))}**\n\n${v}`;
        if (typeof v === "object") return `**${escape(label(k))}**\n\n${jsonBlock(v)}`;
        return `**${escape(label(k))}:** ${escape(v)}`;
      }).join("\n\n")}`;
    }).join("\n\n")}`;
  }
  if (Array.isArray(value) && value.every((item) => item === null || typeof item !== "object")) {
    return `${heading}\n\n${value.map((item) => `- ${escape(item)}`).join("\n")}`;
  }
  return `${heading}\n\n${jsonBlock(value)}`;
}

function issueMarkdown(issue) {
  const summary = ["id", "status", "issue_type", "priority"]
    .filter((key) => nonempty(issue[key]))
    .map((key) => `**${escape(label(key))}:** ${escape(issue[key])}`)
    .join("  \n");
  const sections = Object.entries(issue)
    .filter(([key, value]) => !SUMMARY.has(key) && nonempty(value))
    .map(([key, value]) => field(key, value));
  return [`# ${escape(issue.title ?? "Untitled issue")}`, summary, ...sections].filter(Boolean).join("\n\n");
}

function selection() {
  const p = props() ?? {};
  const value = typeof p.selection === "string" ? shared(p.selection) : p;
  if (!value || typeof value !== "object" || typeof value.id !== "string" || !value.id.trim()) return null;
  return {
    id: value.id.trim(),
    folder: typeof value.folder === "string" ? value.folder : "",
    beads: typeof value.beads === "string" ? value.beads : "",
    machine: typeof value.machine === "string" && value.machine ? value.machine : null,
  };
}
const selectionKey = (value) => JSON.stringify(value);
const errorText = (error) => error instanceof Error ? error.message : String(error);

export default class BeadDetail extends View {
  init(_props, cx) {
    this.handle = cx.focus_handle();
    this.generation = 0;
    this.context = undefined;
    this.issue = null;
    this.markdown = "";
    this.error = null;
    this.loading = false;
    this.synchronize(cx);
    cx.spawn(async (cx) => {
      for (;;) {
        const command = await nextCommand();
        if (command === "focus") this.handle.focus();
        else if (command === "refresh") this.refresh(cx);
        cx.notify();
      }
    });
  }

  synchronize(cx) {
    const selected = selection(), key = selectionKey(selected);
    if (key === this.context) return;
    this.context = key;
    this.selected = selected;
    this.generation++;
    this.issue = null;
    this.markdown = "";
    this.error = null;
    this.loading = false;
    if (selected) this.refresh(cx);
  }

  refresh(cx) {
    const selected = selection();
    if (selectionKey(selected) !== this.context) { this.synchronize(cx); return; }
    if (!selected || this.loading) return;
    if (!selected.folder) { this.error = "This issue selection needs a project folder. Open it from a project Beads dashboard or supply folder in the pane props."; return; }
    const generation = ++this.generation, key = this.context;
    const current = () => this.generation === generation && this.context === key && selectionKey(selection()) === key;
    this.loading = true;
    this.error = null;
    cx.spawn(async (cx) => {
      try {
        const options = { cwd: selected.folder, on: selected.machine };
        let beads = selected.beads;
        if (!beads) {
          const located = await exec("env", ["-u", "BEADS_DIR", "bd", "where", "--json"], options);
          if (!current()) return;
          if (located.code !== 0) throw new Error((located.stderr || located.stdout).trim() || `bd where exited with code ${located.code}`);
          let location;
          try { location = JSON.parse(located.stdout); }
          catch { throw new Error("bd where returned invalid JSON."); }
          if (!location || typeof location.path !== "string" || !location.path) throw new Error("bd where did not return a Beads directory.");
          beads = location.path;
        }
        const result = await exec("env", [`BEADS_DIR=${beads}`, "bd", "show", `--id=${selected.id}`, "--json", "--long", "--brief-deps", "--include-comments", "--include-dependents"], options);
        if (!current()) return;
        if (result.code !== 0) throw new Error((result.stderr || result.stdout).trim() || `bd show exited with code ${result.code}`);
        let data;
        try { data = JSON.parse(result.stdout); }
        catch { throw new Error("bd show returned invalid JSON."); }
        const issue = Array.isArray(data) ? data.find((item) => item?.id === selected.id) : data;
        if (!issue || typeof issue !== "object" || issue.id !== selected.id) throw new Error(`bd show did not return issue ${selected.id}.`);
        this.issue = issue;
        this.markdown = issueMarkdown(issue);
        this.beads = beads;
      } catch (error) {
        if (current()) {
          this.issue = null;
          this.markdown = "";
          this.error = errorText(error);
        }
      } finally {
        if (current()) { this.loading = false; cx.notify(); }
      }
    });
  }

  badge(text, color, t) {
    return div().flex_none().px(7).py(2).rounded(5).bg(t.raised).text_size(11).text_color(color).child(text);
  }

  render(cx) {
    this.synchronize(cx);
    const t = theme(), selected = this.selected, issue = this.issue;
    const statusColor = issue?.status === "closed" ? t.ansi2 : issue?.status === "in_progress" ? t.accent : issue?.status === "blocked" ? t.blocked : t.muted;
    return v_flex().id("bead-detail").size_full().min_w(0).min_h(0).overflow_hidden().bg(t.background).text_color(t.foreground).text_size(13).track_focus(this.handle)
      .on_mouse_down("left", () => this.handle.focus())
      .on_key_down((event, cx) => {
        if (event.key === "r" && !event.modifiers.control && !event.modifiers.platform && !event.modifiers.alt) {
          this.refresh(cx); cx.stop_propagation(); cx.notify();
        }
      })
      .child(h_flex().flex_none().h(44).px(14).gap(8).items_center().border_b(1).border_color(t.border)
        .child(div().font_semibold().child("Bead details"))
        .when(Boolean(selected), (el) => el.child(div().min_w(0).truncate().text_color(t.muted).child(selected?.id ?? "")))
        .child(div().flex_1())
        .child(div().id("bead-detail-refresh").flex_none().px(9).py(5).rounded(6).text_color(selected && !this.loading ? t.foreground : t.dim)
          .when(Boolean(selected && !this.loading), (el) => el.cursor_pointer().hover((s) => s.bg(t.hover)))
          .on_click((_event, cx) => { this.refresh(cx); cx.notify(); })
          .child(this.loading ? "Loading…" : "Refresh")))
      .when(Boolean(issue), (el) => el.child(h_flex().flex_none().px(14).py(8).gap(8).flex_wrap()
        .when(nonempty(issue?.status), (row) => row.child(this.badge(String(issue.status), statusColor, t)))
        .when(nonempty(issue?.issue_type), (row) => row.child(this.badge(String(issue.issue_type), t.muted, t)))
        .when(nonempty(issue?.priority), (row) => row.child(this.badge(`P${issue.priority}`, Number(issue.priority) <= 1 ? t.ansi1 : t.muted, t)))))
      .child(v_flex().id(`bead-detail-body-${this.context}`).flex_1().min_h(0).min_w(0).w_full().overflow_y_scrollbar().p(16).gap(12)
        .when(!selected, (el) => el.child(div().text_color(t.muted).child("Select an issue in the Beads dashboard to read its details. You can keep this pane open while choosing other issues.")))
        .when(Boolean(selected && this.loading && !issue), (el) => el.child(div().text_color(t.muted).child(`Loading ${selected?.id ?? "issue"}…`)))
        .when(Boolean(this.error), (el) => el.child(v_flex().gap(6)
          .child(div().font_semibold().text_color(t.ansi1).child(`Could not load ${selected?.id ?? "issue"}`))
          .child(div().text_color(t.foreground).child(this.error))
          .child(div().text_color(t.muted).child("Refresh to try again."))))
        .when(Boolean(issue), (el) => el.child(TextView.markdown(`bead-detail-document-${this.context}`, this.markdown).w_full().min_w(0).flex_none().selectable(true).scrollable(false)))
        .when(Boolean(issue), (el) => el.child(div().pt(8).text_size(11).text_color(t.dim).child(`${selected?.machine ? `${selected.machine} · ` : ""}${this.beads}`))));
  }
}

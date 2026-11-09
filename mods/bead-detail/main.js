import { View, div } from "gpui-kit";
import { h_flex, v_flex, TextView } from "gpui-base";
import { exec, nextCommand, props, setShared, shared, theme } from "herdr";

const NARRATIVE = new Set(["description", "design", "acceptance_criteria", "notes", "body", "text", "comment"]);
const SUMMARY = new Set(["id", "title", "status", "issue_type", "priority"]);
const METADATA = new Set(["assignee", "owner", "created_by", "labels", "parent", "parent_id", "external_ref", "spec_id", "due", "defer_until", "estimated_minutes", "estimate", "close_reason"]);
const RELATIONSHIPS = new Set(["dependencies", "dependents", "children", "related", "related_issues", "blocked_by", "blocks"]);
const LABELS = { issue_type: "Type", acceptance_criteria: "Acceptance criteria", dependency_type: "Relationship", parent_id: "Parent", external_ref: "External reference", spec_id: "Specification", estimated_minutes: "Estimate (minutes)" };
const label = (key) => LABELS[key] ?? key.replace(/[_-]/g, " ").replace(/^./, (c) => c.toUpperCase());
const priorityLabel = (value) => `P${value}${["Critical", "High", "Medium", "Low", "Backlog"][Number(value)] ? ` · ${["Critical", "High", "Medium", "Low", "Backlog"][Number(value)]}` : ""}`;
const isDate = (key) => key.endsWith("_at") || key.endsWith("_until") || key === "due" || key === "lease_expires";
const simple = (value) => typeof value !== "object" || (Array.isArray(value) && value.every((item) => item === null || typeof item !== "object"));
const metadataKey = (key) => METADATA.has(key) || isDate(key);
const nonempty = (value) => value !== undefined && value !== null && value !== "" &&
  (!Array.isArray(value) || value.length > 0) &&
  (typeof value !== "object" || Array.isArray(value) || Object.keys(value).length > 0);
const escape = (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/[\\`*_{}\[\]()#+.!|~-]/g, "\\$&").replace(/\n/g, "  \n");

function readable(key, value) {
  if (isDate(key) && typeof value === "string") {
    if (value === "0001-01-01T00:00:00Z") return "Not recorded";
    const date = new Date(value);
    if (!Number.isNaN(date.getTime())) return date.toLocaleString();
  }
  if (key === "status" || key === "issue_type" || key === "dependency_type" || key === "type") return label(String(value));
  if (key === "priority") return priorityLabel(value);
  if (Array.isArray(value)) return value.map((item) => String(item)).join(", ");
  return String(value);
}

function inlineField(key, value) {
  const text = readable(key, value);
  const original = isDate(key) && text !== String(value) ? ` (${value})` : "";
  return `**${escape(label(key))}:** ${escape(text + original)}`;
}

function jsonBlock(value) {
  const text = JSON.stringify(value, null, 2);
  const ticks = text.match(/`+/g) ?? [];
  const fence = "`".repeat(Math.max(3, ...ticks.map((s) => s.length + 1)));
  return `${fence}json\n${text}\n${fence}`;
}

function field(key, value, depth = 3) {
  if (!nonempty(value)) return "";
  const heading = `${"#".repeat(Math.min(depth, 6))} ${escape(label(key))}`;
  if (typeof value === "string") return NARRATIVE.has(key) ? `${heading}\n\n${value}` : inlineField(key, value);
  if (typeof value !== "object") return inlineField(key, value);
  if (Array.isArray(value)) {
    if (value.every((item) => item === null || typeof item !== "object")) {
      return `${heading}\n\n${value.map((item) => `- ${escape(item)}`).join("\n")}`;
    }
    return `${heading}\n\n${value.map((item, index) => {
      if (!item || typeof item !== "object" || Array.isArray(item)) return jsonBlock(item);
      return entryMarkdown(item, index, depth + 1);
    }).join("\n\n")}`;
  }
  return `${heading}\n\n${Object.entries(value).filter(([, v]) => nonempty(v)).map(([k, v]) => field(k, v, depth + 1)).join("\n\n")}`;
}

function entryMarkdown(item, index, depth = 3) {
  const identity = ["id", "title"].filter((key) => nonempty(item[key]));
  const title = identity.map((key) => escape(item[key])).join(" · ") || `Entry ${index + 1}`;
  const entries = Object.entries(item).filter(([key, value]) => !identity.includes(key) && nonempty(value));
  const details = entries.filter(([key, value]) => simple(value) && !NARRATIVE.has(key));
  const content = entries.filter(([key, value]) => !simple(value) || NARRATIVE.has(key));
  return [
    `${"#".repeat(Math.min(depth, 6))} ${title}`,
    details.map(([key, value]) => inlineField(key, value)).join("  \n"),
    ...content.map(([key, value]) => field(key, value, depth + 1)),
  ].filter(Boolean).join("\n\n");
}

function issueSections(issue) {
  const entries = Object.entries(issue).filter(([, value]) => nonempty(value));
  const sections = [];
  for (const key of NARRATIVE) {
    if (nonempty(issue[key])) sections.push({ key, title: label(key), markdown: typeof issue[key] === "string" ? issue[key] : field(key, issue[key]) });
  }
  for (const key of RELATIONSHIPS) {
    if (!nonempty(issue[key])) continue;
    sections.push({ key, title: label(key), relationships: issue[key] });
  }
  if (nonempty(issue.comments)) sections.push({ key: "comments", title: "Comments", comments: issue.comments });
  const additional = entries.filter(([key, value]) => !SUMMARY.has(key) && !NARRATIVE.has(key) && !RELATIONSHIPS.has(key) &&
    key !== "comments" && !(metadataKey(key) && simple(value)));
  if (additional.length) sections.push({ key: "additional", title: "Additional details", markdown: additional.map(([key, value]) => field(key, value)).join("\n\n") });
  return sections;
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
    this.inputContext = undefined;
    this.history = [];
    this.historyIndex = -1;
    this.issue = null;
    this.sections = [];
    this.ready = new Set();
    this.error = null;
    this.loading = false;
    this.synchronize(cx);
    cx.spawn(async (cx) => {
      for (;;) {
        const command = await nextCommand();
        if (command === "focus") this.handle.focus();
        else if (command === "refresh") this.refresh(cx);
        else if (command === "back") this.travel(-1, cx);
        else if (command === "forward") this.travel(1, cx);
        else if (command.startsWith("select:")) this.navigate(command.slice(7), cx);
        cx.notify();
      }
    });
  }

  synchronize(cx) {
    const selected = selection(), key = selectionKey(selected);
    if (key === this.inputContext) return;
    this.inputContext = key;
    this.show(selected, cx);
  }

  show(selected, cx, record = true) {
    const key = selectionKey(selected);
    if (key === this.context) return;
    if (record && selected) {
      this.history.splice(this.historyIndex + 1);
      this.history.push(selected);
      this.historyIndex = this.history.length - 1;
    }
    this.context = key;
    this.selected = selected;
    this.generation++;
    this.issue = null;
    this.sections = [];
    this.ready = new Set();
    this.error = null;
    this.loading = false;
    if (selected) this.refresh(cx);
  }

  publish(selected, cx, record = true) {
    const key = props()?.selection;
    if (typeof key === "string") {
      setShared(key, selected);
      this.inputContext = selectionKey(selection());
    }
    this.show(selected, cx, record);
    cx.notify();
  }

  navigate(id, cx) {
    if (this.selected && typeof id === "string" && id.trim()) this.publish({ ...this.selected, id: id.trim() }, cx);
  }

  travel(delta, cx) {
    const index = this.historyIndex + delta;
    if (index < 0 || index >= this.history.length) return;
    this.historyIndex = index;
    this.publish(this.history[index], cx, false);
  }

  refresh(cx) {
    if (selectionKey(selection()) !== this.inputContext) { this.synchronize(cx); return; }
    const selected = this.selected;
    if (!selected || this.loading) return;
    if (!selected.folder) { this.error = "This issue selection needs a project folder. Open it from a project Beads dashboard or supply folder in the pane props."; return; }
    const generation = ++this.generation, key = this.context;
    const current = () => this.generation === generation && this.context === key && selectionKey(selection()) === this.inputContext;
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
        const [result, readyResult] = await Promise.all([
          exec("env", [`BEADS_DIR=${beads}`, "bd", "show", `--id=${selected.id}`, "--json", "--long", "--brief-deps", "--include-comments", "--include-dependents"], options),
          exec("env", [`BEADS_DIR=${beads}`, "bd", "ready", "--limit", "0", "--json"], options),
        ]);
        if (!current()) return;
        if (result.code !== 0) throw new Error((result.stderr || result.stdout).trim() || `bd show exited with code ${result.code}`);
        if (readyResult.code !== 0) throw new Error(`Could not load readiness: ${(readyResult.stderr || readyResult.stdout).trim() || `bd ready exited with code ${readyResult.code}`}`);
        let data, ready;
        try { data = JSON.parse(result.stdout); }
        catch { throw new Error("bd show returned invalid JSON."); }
        try { ready = JSON.parse(readyResult.stdout); }
        catch { throw new Error("bd ready returned invalid JSON."); }
        if (!Array.isArray(ready) || ready.some((item) => !item || typeof item.id !== "string")) throw new Error("bd ready did not return an issue list.");
        const issue = Array.isArray(data) ? data.find((item) => item?.id === selected.id) : data;
        if (!issue || typeof issue !== "object" || issue.id !== selected.id) throw new Error(`bd show did not return issue ${selected.id}.`);
        this.issue = issue;
        this.sections = issueSections(issue);
        this.ready = new Set(ready.map((item) => item.id));
        this.beads = beads;
      } catch (error) {
        if (current()) {
          this.issue = null;
          this.sections = [];
          this.ready = new Set();
          this.error = errorText(error);
        }
      } finally {
        if (current()) { this.loading = false; cx.notify(); }
      }
    });
  }

  badge(text, color, t) {
    return div().flex_none().px(8).py(3).rounded(5).bg(t.raised).text_size(11).font_semibold().text_color(color).child(text);
  }

  markdown(id, text) {
    return TextView.markdown(`bead-detail-${this.context}-${id}`, text).w_full().min_w(0).flex_none().text_size(13).line_height(1.5).selectable(true).scrollable(false);
  }

  issueLink(id, key, t) {
    return div().id(`bead-link-${key}-${id}`).text_size(11).text_color(t.accent).cursor_pointer()
      .tooltip(`Open ${id}`).hover((style) => style.underline())
      .on_click((_event, cx) => { cx.stop_propagation(); this.navigate(id, cx); }).child(id);
  }

  historyButton(direction, title, delta, t) {
    const index = this.historyIndex + delta, enabled = index >= 0 && index < this.history.length;
    return div().id(`bead-detail-${direction}`).flex_none().px(8).py(5).rounded(6)
      .text_color(enabled ? t.foreground : t.dim)
      .tooltip(enabled ? `${title}: ${this.history[index].id}` : title)
      .when(enabled, (el) => el.cursor_pointer().hover((style) => style.bg(t.hover))
        .on_click((_event, cx) => { cx.stop_propagation(); this.travel(delta, cx); }))
      .child(title);
  }

  metadataRow(key, value, t) {
    return h_flex().w_full().min_w(0).flex_none().gap(10).py(3).items_start()
      .child(div().w(105).flex_none().text_size(11).text_color(t.muted).child(label(key)))
      .child(div().flex_1().min_w(0).text_size(12).tooltip(Array.isArray(value) ? value.map(String).join("\n") : String(value))
        .when(key === "spec_id" || key === "external_ref", (el) => el.truncate())
        .when(key === "parent" || key === "parent_id", (el) => el.child(this.issueLink(String(value), key, t)))
        .when(key !== "labels" && key !== "parent" && key !== "parent_id", (el) => el.child(readable(key, value)))
        .when(key === "labels", (el) => el.child(h_flex().min_w(0).gap(5).flex_wrap()
          .children((Array.isArray(value) ? value : [value]).map((item) => this.badge(String(item), t.muted, t))))));
  }

  issueHeader(issue, t) {
    const ready = this.ready.has(issue.id);
    const statusColor = ready ? t.ansi2 : issue.status === "in_progress" ? t.accent : issue.status === "blocked" ? t.blocked : t.muted;
    const metadata = Object.entries(issue).filter(([key, value]) => metadataKey(key) && nonempty(value) && simple(value));
    return v_flex().w_full().min_w(0).flex_none().gap(14).pb(6)
      .child(v_flex().min_w(0).gap(8)
        .child(div().text_size(11).font_semibold().text_color(t.accent).child(issue.id))
        .child(div().min_w(0).text_size(22).line_height(1.25).font_semibold().child(issue.title ?? "Untitled issue"))
        .child(h_flex().min_w(0).gap(6).flex_wrap()
          .when(ready || nonempty(issue.status), (el) => el.child(this.badge(ready ? "Ready" : readable("status", issue.status), statusColor, t)
            .tooltip(`Status: ${issue.status ?? "not provided"}${ready ? "\nReady according to bd ready" : ""}`)))
          .when(nonempty(issue.issue_type), (el) => el.child(this.badge(readable("issue_type", issue.issue_type), t.accent, t).tooltip(`Type: ${issue.issue_type}`)))
          .when(nonempty(issue.priority), (el) => el.child(this.badge(priorityLabel(issue.priority), Number(issue.priority) <= 1 ? t.ansi1 : t.muted, t).tooltip(`Priority: ${issue.priority}`)))))
      .when(metadata.length > 0, (el) => el.child(v_flex().min_w(0).w_full().flex_none().px(12).py(8).rounded(8).bg(t.surface).border(1).border_color(t.border)
        .children(metadata.map(([key, value]) => this.metadataRow(key, value, t)))));
  }

  comments(value, t) {
    if (!Array.isArray(value)) return this.markdown("comments", field("comments", value));
    return v_flex().w_full().min_w(0).flex_none().gap(12).children(value.map((comment, index) => {
      if (!comment || typeof comment !== "object" || Array.isArray(comment)) return this.markdown(`comment-${index}`, typeof comment === "string" ? comment : jsonBlock(comment));
      const authorKey = ["author", "created_by"].find((key) => nonempty(comment[key]) && simple(comment[key]));
      const entries = Object.entries(comment).filter(([key, value]) => key !== authorKey && nonempty(value));
      const metadata = entries.filter(([key, value]) => simple(value) && !NARRATIVE.has(key));
      const content = entries.filter(([key, value]) => !simple(value) || NARRATIVE.has(key));
      return v_flex().min_w(0).w_full().flex_none().p(12).gap(8).rounded(7).bg(t.surface).border(1).border_color(t.border)
        .child(div().font_semibold().text_size(12).child(authorKey ? readable(authorKey, comment[authorKey]) : `Comment ${index + 1}`))
        .children(metadata.map(([key, value]) => this.metadataRow(key, value, t)))
        .children(content.map(([key, value]) => this.markdown(`comment-${index}-${key}`, typeof value === "string" && NARRATIVE.has(key)
          ? (content.length > 1 ? `**${escape(label(key))}**\n\n${value}` : value)
          : field(key, value))));
    }));
  }

  relationships(key, value, t) {
    if (!Array.isArray(value)) return this.markdown(key, field(key, value));
    return v_flex().w_full().min_w(0).flex_none().gap(10).children(value.map((issue, index) => {
      if (typeof issue === "string") return this.issueLink(issue, `${key}-${index}`, t);
      if (!issue || typeof issue !== "object" || Array.isArray(issue)) return this.markdown(`${key}-${index}`, jsonBlock(issue));
      const ready = this.ready.has(issue.id);
      const entries = Object.entries(issue).filter(([key, value]) => !SUMMARY.has(key) && nonempty(value));
      const metadata = entries.filter(([key, value]) => simple(value) && !NARRATIVE.has(key));
      const content = entries.filter(([key, value]) => !simple(value) || NARRATIVE.has(key));
      return v_flex().w_full().min_w(0).flex_none().p(12).gap(8).rounded(7).bg(t.surface).border(1).border_color(t.border)
        .when(nonempty(issue.id), (el) => el.child(this.issueLink(String(issue.id), `${key}-${index}`, t)))
        .when(nonempty(issue.title), (el) => el.child(div().font_semibold().text_size(13).child(String(issue.title))))
        .child(h_flex().min_w(0).gap(6).flex_wrap()
          .when(ready || nonempty(issue.status), (el) => el.child(this.badge(ready ? "Ready" : readable("status", issue.status), ready ? t.ansi2 : issue.status === "closed" ? t.dim : t.muted, t)
            .tooltip(`Status: ${issue.status ?? "not provided"}${ready ? "\nReady according to bd ready" : ""}`)))
          .when(nonempty(issue.issue_type), (el) => el.child(this.badge(readable("issue_type", issue.issue_type), t.muted, t).tooltip(`Type: ${issue.issue_type}`)))
          .when(nonempty(issue.priority), (el) => el.child(this.badge(priorityLabel(issue.priority), t.muted, t).tooltip(`Priority: ${issue.priority}`))))
        .children(metadata.map(([key, value]) => this.metadataRow(key, value, t)))
        .children(content.map(([name, value]) => this.markdown(`${key}-${index}-${name}`, field(name, value))));
    }));
  }

  section(section, t) {
    return v_flex().min_w(0).w_full().flex_none().gap(10).pt(14).border_t(1).border_color(t.border)
      .child(div().text_size(14).font_semibold().child(section.title))
      .child(section.key === "comments" ? this.comments(section.comments, t)
        : RELATIONSHIPS.has(section.key) ? this.relationships(section.key, section.relationships, t)
        : this.markdown(section.key, section.markdown));
  }

  render(cx) {
    this.synchronize(cx);
    const t = theme(), selected = this.selected, issue = this.issue;
    const sections = this.sections;
    return v_flex().id("bead-detail").size_full().min_w(0).min_h(0).overflow_hidden().bg(t.background).text_color(t.foreground).text_size(13).track_focus(this.handle)
      .on_mouse_down("left", () => this.handle.focus())
      .on_key_down((event, cx) => {
        if (event.key === "r" && !event.modifiers.control && !event.modifiers.platform && !event.modifiers.alt) {
          this.refresh(cx); cx.stop_propagation(); cx.notify();
        }
      })
      .child(h_flex().flex_none().h(44).px(14).gap(8).items_center().border_b(1).border_color(t.border)
        .child(div().font_semibold().child("Bead details"))
        .child(this.historyButton("back", "← Back", -1, t))
        .child(this.historyButton("forward", "Forward →", 1, t))
        .child(div().flex_1())
        .child(div().id("bead-detail-refresh").flex_none().px(9).py(5).rounded(6).text_color(selected && !this.loading ? t.foreground : t.dim)
          .when(Boolean(selected && !this.loading), (el) => el.cursor_pointer().hover((s) => s.bg(t.hover)))
          .on_click((_event, cx) => { this.refresh(cx); cx.notify(); })
          .child(this.loading ? "Loading…" : "Refresh")))
      .child(v_flex().id(`bead-detail-body-${this.context}`).flex_1().min_h(0).min_w(0).w_full().overflow_y_scrollbar().p(16).gap(18)
        .when(!selected, (el) => el.child(div().text_color(t.muted).child("Select an issue in the Beads dashboard to read its details. You can keep this pane open while choosing other issues.")))
        .when(Boolean(selected && this.loading && !issue), (el) => el.child(div().text_color(t.muted).child(`Loading ${selected?.id ?? "issue"}…`)))
        .when(Boolean(this.error), (el) => el.child(v_flex().gap(6)
          .child(div().font_semibold().text_color(t.ansi1).child(`Could not load ${selected?.id ?? "issue"}`))
          .child(div().text_color(t.foreground).child(this.error))
          .child(div().text_color(t.muted).child("Refresh to try again."))))
        .when(Boolean(issue), (el) => el.child(this.issueHeader(issue, t)).children(sections.map((section) => this.section(section, t))))
        .when(Boolean(issue), (el) => el.child(div().min_w(0).pt(8).text_size(11).text_color(t.dim).tooltip(`${selected?.machine ? `${selected.machine} · ` : ""}${this.beads}`)
          .child(`${selected?.machine ? `${selected.machine} · ` : ""}${this.beads}`))));
  }
}

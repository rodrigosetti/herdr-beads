import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { focused, layout, nextCommand, panes, run, spaces, theme } from "herdr";

function target() {
  const space = spaces().find((item) => item.active);
  if (!space) return null;
  const tabs = new Set(space.tabs.map((tab) => tab.id));
  const all = panes().filter((pane) => tabs.has(pane.tab));
  return {
    space,
    dashboard: all.find((pane) => pane.mod === "beads-dashboard"),
    folder: all.find((pane) => pane.terminal && pane.cwd)?.cwd || space.folder,
  };
}

export default class BeadsSidebar extends View {
  init(_props, cx) {
    cx.spawn(async (cx) => {
      for (;;) {
        if (await nextCommand() === "open") this.open();
        cx.notify();
      }
    });
  }

  open() {
    const selected = target();
    if (!selected) return;
    if (selected.dashboard) {
      run("tab.select", { id: selected.dashboard.tab });
      run("pane.focus", { pane: selected.dashboard.pane });
    } else {
      run("tab.new", {
        name: "Beads",
        space: selected.space.id,
        mod: "beads-dashboard",
        props: { folder: selected.folder, machine: selected.space.machine },
      });
    }
  }

  render() {
    const t = theme(), selected = target();
    const collapsed = (layout()?.children?.find((node) => node.id === "sidebar")?.size?.px ?? 268) < 160;
    const active = !!selected?.dashboard && selected.dashboard.tab === focused()?.tab;
    return v_flex().size_full().bg(t.chrome).border_t(1).border_color(t.border).px(8).py(4).text_size(13)
      .child(h_flex().id("open-space-beads").h(28).px(8).gap(9).items_center().rounded(8)
        .text_color(!selected ? t.dim : active ? t.accent : t.muted)
        .when(active, (element) => element.bg(t.selected))
        .when(!!selected, (element) => element.cursor_pointer().hover((style) => style.bg(t.hover).text_color(t.foreground)))
        .tooltip(selected ? `Open Beads for ${selected.space.name}` : "Select a space to open Beads")
        .on_click(() => this.open())
        .child(svg("mods/workspace-chrome/layers.svg").size(14).flex_none())
        .when(!collapsed, (element) => element.child(div().child("Beads"))));
  }
}

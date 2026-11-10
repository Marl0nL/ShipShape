// A pane's header; props: { pane, terminal }.

import { div } from "gpui-kit";
import { h_flex } from "gpui-base";
import { panes, props, run, home, theme } from "herdr";
import { pip, icon, programIcon } from "./kit.js";

function folder(cwd) {
  if (!cwd) return "";
  const h = home();
  return cwd === h ? "~" : cwd.startsWith(h + "/") ? "~" + cwd.slice(h.length) : cwd;
}

export default class Pane {
  render() {
    const t = theme();
    const p = panes().find((x) => x.pane === props()?.pane);
    if (!p) return div();
    const pane = p.pane;
    const where = folder(p.cwd);
    const title = p.title || "shell";
    // Shells often put the folder in their title already.
    const dir = where && !title.includes(where) && !/^[~/]/.test(title) ? where : "";
    const button = (name, command, args) =>
      div()
        .id(`ph-${name}-${pane}`)
        .p(4)
        .rounded(5)
        .cursor_pointer()
        .hover((st) => st.bg(t.hover))
        .on_click((_e, cx) => {
          cx.stop_propagation();
          run(command, args);
        })
        .child(icon(name, 14, t.dim));
    return h_flex()
      .id(`ph-${pane}`)
      .size_full()
      .pl(11)
      .pr(6)
      .gap(8)
      .items_center()
      .text_size(12.5)
      .bg(t.chrome)
      .border_b_1()
      .border_color(t.border)
      .on_click(() => run("pane.focus", { pane }))
      .child(programIcon(p.program, 14, p.focused ? t.accent : t.muted) ?? div())
      .when(Boolean(p.status), (el) => el.child(pip(t, p.status, 6)))
      .child(div().flex_none().max_w(340).truncate().font_semibold().text_color(p.focused ? t.foreground : t.muted).child(title))
      .child(div().flex_1().min_w(0).truncate().text_color(t.dim).child(dir))
      .when(Boolean(p.branch), (el) => el.child(div().flex_none().max_w(160).truncate().text_size(11).text_color(t.dim).child(p.branch)))
      .child(button("split-right", "pane.split", { direction: "right", pane }))
      .child(button("split-down", "pane.split", { direction: "down", pane }))
      .child(button(p.zoomed ? "unzoom" : "zoom", "pane.zoom", { pane }))
      .child(button("close", "pane.close", { pane }));
  }
}

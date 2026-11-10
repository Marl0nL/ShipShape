// The muster roll: every agent in the fleet, the ones that need you first.
// alt-up / alt-down (agent.prev / agent.next) walk it in this order.

import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { agents, spaces, run, theme, setAgentOrder } from "herdr";
import { FELL, WORDS, ORDER, pip, programIcon, clean, show } from "./kit.js";

function ago(ms) {
  const m = Math.floor(ms / 60000);
  if (m < 1) return "<1m";
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h}h` : `${Math.floor(h / 24)}d`;
}

export default class Muster {
  init(_p, cx) {
    // The watch times only.
    cx.timer.every(30000, (cx) => cx.notify());
  }

  render() {
    const t = theme();
    const all = agents();
    const since = new Map();
    for (const s of spaces()) for (const tab of s.tabs) since.set(tab.id, tab.working_since);
    const groups = ORDER.map((status) => ({ status, list: all.filter((a) => a.status === status).sort((a, b) => b.seq - a.seq) })).filter((g) => g.list.length);
    setAgentOrder(groups.flatMap((g) => g.list.map((a) => a.address)));
    const now = Date.now();

    const row = (a) => {
      const name = clean(a.title) || a.tab_name || a.program?.name || a.address;
      const watch = a.status === "working" && since.get(a.tab) ? ago(now - since.get(a.tab)) : "";
      return h_flex()
        .id(`hand-${a.address}`)
        .flex_none()
        .h(27)
        .mx(6)
        .px(8)
        .gap(8)
        .items_center()
        .rounded(6)
        .cursor_pointer()
        .text_size(12.5)
        .when(a.focused, (el) => el.bg(t.selected))
        .hover((st) => st.bg(t.hover))
        .on_click(() => {
          show(a.space);
          run("pane.focus", { pane: a.pane });
        })
        .child(pip(t, a.status))
        .child(programIcon(a.program, 13, t.muted) ?? div())
        .child(div().flex_1().min_w(0).truncate().text_color(a.focused ? t.foreground : t.muted).child(name))
        .child(div().flex_none().max_w(120).truncate().text_size(11).text_color(t.dim).child(a.space_name))
        .when(Boolean(watch), (el) => el.child(div().flex_none().w(26).text_size(11).text_color(t.working).child(watch)));
    };

    return v_flex()
      .size_full()
      .bg(t.chrome)
      .border_t_1()
      .border_color(t.border)
      .child(
        h_flex()
          .flex_none()
          .h(30)
          .px(14)
          .items_center()
          .justify_between()
          .child(div().font_family(FELL).italic().text_size(15).text_color(t.foreground).child("Muster Roll"))
          .child(div().text_size(10.5).text_color(t.dim).child(all.length ? `${all.length} ${all.length === 1 ? "hand" : "hands"}` : "")),
      )
      .child(
        v_flex()
          .id("muster-list")
          .flex_1()
          .min_h(0)
          .pb(6)
          .overflow_y_scrollbar()
          .when(!all.length, (el) => el.child(div().px(14).py(6).text_size(12).text_color(t.dim).child("No hands aboard. Start an agent with + at the helm.")))
          .children(
            groups.flatMap((g) => [
              h_flex()
                .flex_none()
                .h(20)
                .px(14)
                .gap(6)
                .items_center()
                .text_size(10)
                .text_color(g.status === "idle" ? t.dim : t[g.status])
                .child(WORDS[g.status].toUpperCase())
                .child(div().text_color(t.dim).child(String(g.list.length))),
              ...g.list.map(row),
            ]),
          ),
      );
  }
}

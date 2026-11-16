// The Fleet panel: your Ships as FleetLedger last saw them, in a Herdr tab. It only reads.
// All its logic is in model.js; this file draws and hands model.js Herdr's functions.
//
//   herdr-app do tab.new '{"name":"Fleet","mod":"fleet"}'   open it as a tab
//   fleet.open      (keys.json or `herdr-app do`) the Fleet tab, or a new one
//   fleet.refresh   read FleetLedger again now
//
// See README.md beside it: settings, what it runs and where.

import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { exec, hc, run, theme, spaces, panes, machines, readTextFile, home, appHome, nextCommand } from "herdr";
import { Fleet, rows, header, isShown, openPlan, selectPlan, settingsPath } from "./model.js";

const host = { exec, hc, readTextFile, home, appHome };
// One model for every copy of the view, so two Fleet tabs make one read, not two.
const fleet = new Fleet(host);
const TICK_MS = 15000;

const stop = (_e, cx) => cx.stop_propagation();
const toneColor = (t, tone) => ({ good: t.done, warn: t.ansi3 ?? t.working, bad: t.blocked, dim: t.dim })[tone] ?? t.muted;

// A steady colour per tag, from the theme's own ANSI colours (as the Desk does).
function tagColor(t, tag) {
  let h = 0;
  for (const ch of tag) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const pool = [t.ansi4, t.ansi5, t.ansi6, t.ansi2, t.ansi3, t.ansi1, t.ansi12, t.ansi13].filter(Boolean);
  return pool[h % pool.length] ?? t.accent;
}

function chip(t, text, color) {
  return h_flex()
    .flex_none()
    .h(18)
    .px(6)
    .gap(4)
    .items_center()
    .rounded(6)
    .bg(`${color}26`)
    .border_1()
    .border_color(`${color}55`)
    .text_size(11)
    .text_color(t.foreground)
    .child(div().size(6).flex_none().rounded(3).bg(color))
    .child(text);
}

function button(t, id, text, act) {
  return h_flex()
    .id(id)
    .flex_none()
    .h(24)
    .px(8)
    .items_center()
    .rounded(6)
    .border_1()
    .border_color(t.border)
    .text_size(11.5)
    .text_color(t.muted)
    .cursor_pointer()
    .hover((s) => s.bg(t.hover).text_color(t.foreground))
    .on_mouse_down("left", stop)
    .on_click((e, cx) => {
      act(e, cx);
      cx.notify();
    })
    .child(text);
}

export default class FleetPanel extends View {
  init(_props, cx) {
    this.note = null;
    cx.timer.every(TICK_MS, (cx) => this.tick(cx));
    this.tick(cx);
    cx.spawn(async (cx) => {
      for (;;) {
        let c;
        try {
          c = await nextCommand();
        } catch {
          return; // this copy is going away (a reload)
        }
        if (c === "open") for (const [name, args] of openPlan(panes(), spaces())) run(name, args);
        if (c === "refresh") this.refresh(cx);
        cx.notify();
      }
    });
  }

  // Reads only while on show: when the last read is POLL_MS old, or there is none yet.
  tick(cx) {
    if (!isShown(panes(), spaces())) return;
    if (fleet.due()) this.refresh(cx);
    else cx.notify(); // "read 4 min ago" moves on
  }

  refresh(cx) {
    cx.spawn(async (cx) => {
      const reading = fleet.refresh();
      cx.notify(); // "asking…"
      await reading;
      cx.notify();
    });
  }

  row(t, r) {
    return h_flex()
      .id(`ship-${r.name}`)
      .h(30)
      .px(8)
      .gap(10)
      .items_center()
      .rounded(6)
      .cursor_pointer()
      .hover((s) => s.bg(t.hover))
      .on_mouse_down("left", stop)
      .on_click((_e, cx) => {
        const plan = selectPlan(r);
        if (plan.run) run(...plan.run);
        this.note = plan.note ?? null;
        cx.notify();
      })
      .child(div().flex_none().w(130).truncate().text_color(r.named ? t.foreground : t.blocked).child(r.name))
      .child(h_flex().flex_none().w(150).gap(4).overflow_hidden().children(r.tags.map((tag) => chip(t, tag, tagColor(t, tag)))))
      .child(div().flex_none().w(110).truncate().text_color(toneColor(t, r.tone)).child(r.state))
      .child(div().flex_none().w(100).truncate().text_color(toneColor(t, r.conn.tone)).child(r.conn.text))
      .child(div().flex_1().min_w(0).truncate().text_color(t.muted).child(r.note));
  }

  render() {
    const t = theme();
    const st = fleet.state;
    const h = header(st, Date.now(), { settings: settingsPath(host) });
    const list = rows(st, { spaces: spaces(), machines: machines() });
    const notice = (n, i) => div().id(`notice-${i}`).w_full().text_size(12).text_color(toneColor(t, n.tone)).child(n.text);
    return v_flex()
      .id("fleet")
      .size_full()
      .min_h(0)
      .bg(t.canvas)
      .p(12)
      .gap(8)
      .text_size(13)
      .text_color(t.foreground)
      .child(
        h_flex()
          .w_full()
          .gap(8)
          .items_center()
          .child(div().flex_none().text_size(14).font_weight(600).child("Fleet"))
          .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(t.dim).child(h.facts.join(" · ")))
          .child(button(t, "refresh", st.asking ? "Reading…" : "Refresh", (_e, cx) => this.refresh(cx))),
      )
      .when(h.locked, (el) => el.child(div().h(2).w_full().rounded(1).bg(t.blocked)))
      .children(h.notices.map(notice))
      .when(Boolean(this.note), (el) => el.child(div().text_size(12).text_color(t.muted).child(this.note ?? "")))
      .when(Boolean(h.empty) && !list.length, (el) => el.child(div().pt(8).text_color(t.dim).child(h.empty ?? "")))
      .child(v_flex().id("ships").flex_1().min_h(0).overflow_y_scrollbar().children(list.map((r) => this.row(t, r))));
  }
}

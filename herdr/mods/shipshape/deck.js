// The deck view: one ship up close, filling the chart.
//   sails    each one set carries the name and task of an agent that's working; click it to go to them
//   helm     the wheel is the helmsman's: ShipShape's primary agent (see kit.js); click it to go to them
//   papers   the ship's particulars; her vitals (the VM's own figures) will stand here once ShipShape reports them
//   roll     everyone aboard, along the bottom
// Scroll out, or press Chart, to go back.

import { div, image } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { run, spaces, panes } from "herdr";
import { C, DIR, FELL, CLASSES, WORDS, muster, berthName, helmsman, goalOf, clean, ago, pip, icon, programIcon } from "./kit.js";
import { CLOSE } from "./sprites.js";

const SEA = "#0b2a3a";
const INK = "#2a1a0c";
const png = (name) => image(`${DIR}/img/${name}.png`);

// FleetLedger's web UI, in a tab on this computer: `ship-fleet ui` serves it and opens the browser.
function papers() {
  const here = spaces().find((s) => !s.machine);
  if (here) run("tab.new", { name: "FleetLedger", space: here.id, argv: ["ship-fleet", "ui"] });
  else run("space.new", { name: "FleetLedger", machine: "", argv: ["ship-fleet", "ui"] });
}

export function deckView(root, { W, H, lit = 0 }, water, ship, { port, harbour, ledger, moving }, t, wash, leave) {
  const cls = CLASSES[ship.cls];
  const sp = CLOSE[ship.cls];
  const helm = helmsman(ship);
  const roll = H > 420 ? 64 : 0;
  // As big as fits between the top of the chart and the crew's roll.
  const k = Math.min((W * 0.98) / sp.w, ((H - roll - 30) * 1.02) / sp.h, 1.25);
  const x0 = (W - sp.w * k) / 2;
  const y0 = Math.max(24, (H - roll - sp.h * k) / 2 + 14);
  const go = (a) => run("pane.focus", { pane: a.pane });
  const name = (a) => goalOf(a) ?? (clean(a.title) || a.tab_name || a.program?.name || "a hand");
  // Sails are set in order, one for each agent working: the same order names them.
  const working = ship.crew.filter((a) => a.status === "working").sort((a, b) => a.tab_index - b.tab_index);
  const since = Math.min(...ship.tabs.map((tab) => tab.working_since).filter(Boolean));
  const where = panes().find((p) => ship.crew.some((a) => a.pane === p.pane) && p.cwd)?.cwd;

  // Where the helm and sails are with this many sails set (she heels); older sprites keep one list.
  const slots = Array.isArray(sp.sails[0]?.[0]) ? sp.sails[Math.min(ship.sails, sp.sails.length - 1)] : sp.sails;
  const wheel = Array.isArray(sp.helm[0]) ? sp.helm[Math.min(ship.sails, sp.helm.length - 1)] : sp.helm;
  const sails = slots.slice(0, ship.sails).map(([sx, sy, sw], i) => {
    const a = working[i];
    if (!a) return null;
    const w = Math.max(84, sw * k * 0.92);
    return v_flex()
      .id(`sail-${a.address}`)
      .absolute()
      .left(sx * k - w / 2)
      .top(sy * k - 15)
      .w(w)
      .items_center()
      .px(4)
      .py(2)
      .rounded(5)
      .cursor_pointer()
      .hover((st) => st.bg("#00000026"))
      .tooltip(`${name(a)} · ${a.tab_name ? `tab ${a.tab_name} · ` : ""}working. Click to go to them.`)
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .on_click(() => go(a))
      .child(div().max_w(w - 8).truncate().font_semibold().text_size(clamp(10.5 + k * 3, 11, 14)).text_color(INK).child(name(a)))
      .child(div().max_w(w - 8).truncate().text_size(9.5).text_color(`${INK}c0`).child(a.program?.name ?? "agent"));
  });

  const fact = (label, value) =>
    h_flex().gap(8).text_size(11.5).child(div().flex_none().w(74).text_color(`${C.cream}90`).child(label)).child(div().flex_1().min_w(0).truncate().text_color(C.cream).child(value));
  const button = (id, name, label, tip, act) =>
    h_flex()
      .id(id)
      .h(26)
      .px(9)
      .gap(6)
      .items_center()
      .rounded(6)
      .bg(`${SEA}e0`)
      .border_1()
      .border_color(`${C.cream}30`)
      .cursor_pointer()
      .text_size(11.5)
      .text_color(C.cream)
      .hover((st) => st.bg(`${C.brass}40`))
      .tooltip(tip)
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .on_click((_e, cx) => {
        act();
        cx.notify();
      })
      .child(icon(name, 13, C.brass))
      .child(label);

  return root
    // Turning the wheel back takes you up to the chart again.
    .on_scroll_wheel((e, cx) => {
      const dy = e.delta_lines ? e.delta_lines.y : e.delta.y;
      if (dy < 0) {
        leave();
        cx.notify();
      }
    })
    .children(water)
    .child(
      div()
        .absolute()
        .w(sp.w * k)
        .h(sp.h * k)
        // A slow heave and a sway, on beats that don't line up.
        .loop("top", { from: y0 - 2, to: y0 + 2.5, period: 6100 })
        .loop("left", { from: x0 - 3, to: x0 + 3, period: 4700 })
        .child(png(`close-${ship.cls}-${ship.sails}`).w(sp.w * k).h(sp.h * k))
        .children(sails.filter(Boolean))
        // Gulls about the mastheads.
        .children(
          [0, 1, 2].map((i) =>
            div()
              .absolute()
              .top(sp.h * k * 0.06 + i * 14)
              .w(20)
              .h(60)
              // Soaring in figure-eights (across on one beat, up and down on half of it), not flying backwards.
              .loop("left", { from: sp.w * k * (0.3 + i * 0.1), to: sp.w * k * (0.62 + i * 0.08), period: 13000 + i * 4100 })
              .child(image(`${DIR}/img/gull-up.png`).absolute().left(0).w(26 - i * 3).h(12 - i * 1.3).loop("top", { from: 0, to: 34 + i * 10, period: (13000 + i * 4100) / 2 })),
          ),
        )
        // The helm.
        .child(
          div()
            .id("helm-wheel")
            .absolute()
            .left(wheel[0] * k - 19)
            .top(wheel[1] * k - 19)
            .w(38)
            .h(38)
            .rounded_full()
            .border_2()
            .border_color(C.brass)
            .bg(`${SEA}d0`)
            .flex()
            .items_center()
            .justify_center()
            .cursor_pointer()
            .hover((st) => st.bg(`${C.brass}60`))
            .tooltip(helm ? `The helm: ${name(helm)} (${WORDS[helm.status].toLowerCase()}). Click to take the wheel.` : "The helm: no one aboard to take it")
            .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
            .on_click(() => helm && go(helm))
            .child(icon("wheel", 20, C.brass).loop("opacity", { from: 0.6, to: 1, period: 2200 })),
        ),
    )
    .child(div().absolute().left(0).top(0).w(W).h(H).bg(wash))
    // Her lanterns and windows after dark.
    .when(lit > 0.05 && Boolean(sp.lamps), (el) =>
      el.child(div().absolute().w(sp.w * k).h(sp.h * k).opacity(lit).loop("top", { from: y0 - 2, to: y0 + 2.5, period: 6100 }).loop("left", { from: x0 - 3, to: x0 + 3, period: 4700 }).child(png(`close-${ship.cls}-L${ship.sails}`).w(sp.w * k).h(sp.h * k))),
    )
    // Her papers.
    .child(
      v_flex()
        .absolute()
        .left(14)
        .top(12)
        .w(Math.min(270, W - 28))
        .p(11)
        .gap(3)
        .rounded(8)
        .bg(`${SEA}e0`)
        .border_1()
        .border_color(`${C.cream}30`)
        .child(div().truncate().font_family(FELL).italic().text_size(20).text_color(C.cream).child(ship.name))
        .child(div().pb(4).text_size(11).text_color(C.brass).child(`${cls.name} · ${berthName(ship.machine)}`))
        .child(fact("Bound for", `${port.name}${ship.goal ? `: ${ship.goal}` : ""}`))
        .child(fact("State", `${WORDS[ship.state]}${Number.isFinite(since) && ship.working ? `, ${ago(Date.now() - since)}` : ""}`))
        .child(fact("Hands", muster(ship)))
        .child(fact("Helm", helm ? name(helm) : "no one aboard"))
        .when(Boolean(ship.branch), (el) => el.child(fact("Branch", ship.branch)))
        .when(Boolean(where), (el) => el.child(fact("Folder", where)))
        .child(fact("Harbour", `${harbour}${ledger?.tags?.length ? ` (${ledger.tags.join(", ")})` : ""}`))
        // Her vitals: what FleetLedger last knew of the VM (ship-fleet ls --offline).
        .when(Boolean(ledger?.host), (el) => {
          const v = ledger.host;
          return el
            .child(div().mt(5).pt(5).border_t_1().border_color(`${C.cream}22`).child(fact("Load", `${v.load1} on ${v.cpus} cores`)))
            .child(fact("Memory", `${Math.round(v.mem_used_pct)}% of ${Math.round(v.mem_total_mb / 1024)} GB`))
            .child(fact("Disk", `${Math.round(v.disk_used_pct)}% of ${Math.round(v.disk_total_gb)} GB`))
            .child(fact("Afloat", ago(v.uptime_s * 1000)))
            .children((ledger.attention ?? []).slice(0, 3).map((a) => div().pt(2).text_size(10.5).text_color(t.blocked).child(a.message)));
        })
        .when(!ledger?.host, (el) => el.child(div().mt(5).pt(5).border_t_1().border_color(`${C.cream}22`).text_size(10).text_color(`${C.cream}70`).child(ship.machine ? "Vitals: FleetLedger has nothing on this ship yet." : "Vitals: this computer isn't a Ship, so FleetLedger keeps none."))),
    )
    .child(
      h_flex()
        .absolute()
        .top(12)
        .left(W - 14 - 208)
        .w(208)
        .justify_end()
        .gap(6)
        .child(button("deck-papers", "command", "Papers", "The ship's papers: FleetLedger's web UI, opened in a tab on this computer (ship-fleet ui)", papers))
        .child(button("deck-chart", "chart", "Chart", "Back up to the chart (or scroll out)", leave)),
    )
    // The roll: everyone aboard.
    .when(roll > 0, (el) =>
      el.child(
        h_flex()
          .id("deck-roll")
          .absolute()
          .left(10)
          .top(H - roll - 4)
          .w(W - 20)
          .h(roll)
          .gap(5)
          .flex_wrap()
          .items_end()
          .overflow_hidden()
          .children(
            ship.crew.map((a) =>
              h_flex()
                .id(`roll-${a.address}`)
                .h(26)
                .max_w(220)
                .px(8)
                .gap(6)
                .items_center()
                .rounded(6)
                .bg(`${SEA}e0`)
                .border_1()
                .border_color(a === helm ? `${C.brass}a0` : `${C.cream}26`)
                .cursor_pointer()
                .text_size(11.5)
                .hover((st) => st.bg(`${C.brass}40`))
                .tooltip(`${name(a)} · ${WORDS[a.status].toLowerCase()}${a === helm ? " · at the helm" : ""}`)
                .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
                .on_click(() => go(a))
                .child(pip(t, a.status, 6))
                .when(a === helm, (c) => c.child(icon("wheel", 12, C.brass)))
                .child(programIcon(a.program, 12, C.cream) ?? div())
                .child(div().truncate().text_color(C.cream).child(name(a))),
            ),
          )
          .when(!ship.crew.length, (c) => c.child(div().px(8).text_size(11.5).text_color(`${C.cream}90`).child("No hands aboard: start an agent with + at the helm bar."))),
      ),
    );
}

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}

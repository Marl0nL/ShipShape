// The helm: the draggable top bar. The shown ship, its tabs, the fleet's
// tally, the chart's controls and (where the system draws none) window buttons.

import { div } from "gpui-kit";
import { h_flex } from "gpui-base";
import { run, theme, openOverlay, layout, settings, shared, setShared, nextCommand, window as win } from "herdr";

import { FELL, CLASSES, fleet, muster, pip, icon, berthName, show, find } from "./kit.js";

const CHART = 460; // the chart room's usual width
// The chart keeps the clock's hour unless the helm sets one (shared "shipshape.hour").
const HOURS = [[null, "the clock's hour"], [6.2, "dawn"], [12, "noon"], [18.6, "dusk"], [23, "night"]];

// A control in the draggable bar keeps its own presses.
const own = (el) => el.on_mouse_down("left", (_e, cx) => cx.stop_propagation()).on_mouse_down("right", (_e, cx) => cx.stop_propagation());

export default class Helm {
  init(_p, cx) {
    this.width = 1200;
    // shipshape.dawn / .noon / .dusk / .night set the chart's hour; shipshape.clock gives it back to the clock.
    cx.spawn(async (cx) => {
      for (;;) {
        let c;
        try {
          c = await nextCommand();
        } catch {
          return; // this copy is going away (a reload)
        }
        // shipshape.aboard goes aboard the shown ship (the deck view); shipshape.chart comes back up.
        if (c === "aboard") setShared("shipshape.deck", fleet().find((s) => s.active)?.id ?? null);
        if (c === "chart") setShared("shipshape.deck", null);
        // shipshape.watch lets the selected ship go (the console shows every agent's last word); shipshape.ship selects the shown space's ship again.
        if (c === "watch") show(null);
        if (c === "ship") show(fleet().find((s) => s.active)?.id ?? null);
        // shipshape.fleet / shipshape.home point the chart's camera.
        if (c === "fleet" || c === "home") setShared("shipshape.look", { where: c, n: Date.now() });
        const hour = { dawn: 6.2, noon: 12, dusk: 18.6, night: 23, clock: null }[c];
        if (hour !== undefined) setShared("shipshape.hour", hour);
        cx.notify();
      }
    });
  }

  render() {
    const t = theme();
    const w = win() ?? {};
    const ships = fleet();
    const ship = ships.find((s) => s.active);
    const room = find(layout(), "chartroom");
    const zoom = Number(settings()?.zoom) || 1;
    const wide = (room?.size?.px ?? CHART) > CHART + 40;

    const button = (id, name, tip, onClick, on = false) =>
      own(div())
        .id(id)
        .flex_none()
        .p(6)
        .rounded(7)
        .cursor_pointer()
        .text_color(on ? t.accent : t.dim)
        .hover((st) => st.bg(t.raised).text_color(t.foreground))
        .tooltip(tip)
        .on_click(onClick)
        .child(icon(name, 16, on ? t.accent : t.dim));

    const windowButton = (id, name, command) =>
      own(div())
        .id(id)
        .flex_none()
        .w(24)
        .h(24)
        .rounded_full()
        .flex()
        .items_center()
        .justify_center()
        .bg(t.raised)
        .cursor_pointer()
        .hover((st) => st.bg(t.input))
        .on_click(() => run(command))
        .child(icon(name, 14, t.muted));

    const tab = (c) =>
      own(h_flex())
        .id(`tab-${c.id}`)
        .flex_shrink(1)
        .min_w(0)
        .max_w(200)
        .h(26)
        .px(10)
        .gap(7)
        .items_center()
        .rounded(7)
        .border_1()
        .cursor_pointer()
        .text_size(12.5)
        .when(c.active, (el) => el.bg(t.raised).border_color(`${t.accent}80`).text_color(t.foreground))
        .when(!c.active, (el) => el.border_color("#00000000").text_color(t.muted).hover((st) => st.bg(t.hover)))
        .on_click(() => {
          show(ship.id);
          run("tab.select", { id: c.id });
        })
        .on_mouse_down("right", () => openOverlay({ mod: "menu", kind: "popup", width: 200, props: { tab: "tab", target: { id: c.id, space: ship.id } } }))
        .when(Boolean(c.status), (el) => el.child(pip(t, c.status, 6)))
        .child(div().truncate().child(c.name));

    const tally = (name, n, color, tip) =>
      h_flex().id(`tally-${name}`).flex_none().gap(4).items_center().text_size(12).text_color(n ? color : t.dim).tooltip(tip).child(icon(name, 14, n ? color : t.dim)).child(String(n));

    const count = (state) => ships.filter((s) => s.state === state).length;

    return h_flex()
      .id("helm")
      .size_full()
      .px(10)
      .gap(8)
      .items_center()
      .bg(t.chrome)
      .border_b_1()
      .border_color(t.border)
      .on_size_change((size, cx) => {
        this.width = size.width;
        cx.notify();
      })
      .when(w.platform === "macos" && w.titlebar === "none" && !w.fullscreen, (el) => el.pl(78))
      .child(icon("wheel", 17, t.accent))
      .child(div().flex_none().font_family(FELL).text_size(17).text_color(t.foreground).child("ShipShape"))
      .child(div().flex_none().w(1).h(18).mx(4).bg(t.border))
      .when(Boolean(ship), (el) =>
        el
          .child(
            own(h_flex())
              .id("helm-ship")
              .flex_none()
              .h(28)
              .px(10)
              .gap(7)
              .items_center()
              .rounded(7)
              .bg(t.surface)
              .border_1()
              .border_color(t.border)
              .cursor_pointer()
              .text_size(12.5)
              .hover((st) => st.border_color(t.input))
              .tooltip(`${CLASSES[ship.cls].name} on ${berthName(ship.machine)} · ${muster(ship)}. Click to pick another ship.`)
              .on_click(() => openOverlay({ mod: "picker", kind: "popup", at: "top", offset: 40, width: 640, height: 0.8 }))
              .on_mouse_down("right", () => openOverlay({ mod: "menu", kind: "popup", width: 200, height: 260, props: { tab: "space", target: { id: ship.id } } }))
              .child(icon(ship.working ? "sail" : "anchor", 14, t.accent))
              .child(div().max_w(180).truncate().font_semibold().text_color(t.foreground).child(ship.name))
              .child(div().text_size(11).text_color(t.dim).child(CLASSES[ship.cls].name.toLowerCase())),
          )
          .child(h_flex().flex_shrink(1).min_w(0).gap(3).items_center().children(ship.tabs.map(tab)))
          .child(button("helm-new", "plus", "New tab: a terminal or an agent", () => openOverlay({ mod: "new-menu", kind: "popup", width: 250, height: 512 }))),
      )
      .child(div().flex_1())
      .child(
        h_flex()
          .flex_none()
          .gap(11)
          .px(6)
          .child(tally("sail", count("working"), t.working, "Ships under sail: agents working"))
          .child(tally("flare", count("blocked"), t.blocked, "Ships awaiting orders: an agent needs you"))
          .child(tally("lamp", count("done"), t.done, "Ships with cargo landed: work finished, not yet looked at"))
          .child(tally("anchor", count("idle"), t.muted, "Ships at anchor: nothing in flight")),
      )
      .child(div().flex_none().w(1).h(18).mx(2).bg(t.border))
      .child(
        button(
          "helm-hour",
          "sun",
          `The chart shows ${(HOURS.find((h) => h[0] === (shared("shipshape.hour") ?? null)) ?? HOURS[0])[1]}. Click for the next: dawn, noon, dusk, night, then back to the clock.`,
          () => {
            const i = HOURS.findIndex((h) => h[0] === (shared("shipshape.hour") ?? null));
            setShared("shipshape.hour", HOURS[(i + 1) % HOURS.length][0]);
          },
          typeof shared("shipshape.hour") === "number",
        ),
      )
      .child(button("helm-chart", "chart", room?.hidden ? "Unroll the chart" : "Roll up the chart", () => run("layout.toggle", { id: "chartroom" }), !room?.hidden))
      .child(
        button(
          "helm-wide",
          "zoom",
          wide ? "Chart back to the side" : "Spread the chart across the table",
          () => {
            run("layout.show", { id: "chartroom" });
            run("layout.resize", { id: "chartroom", px: wide ? CHART : Math.round((this.width / zoom) * 0.62) });
          },
          wide,
        ),
      )
      .child(button("helm-keys", "command", "Command palette", () => run("app.palette")))
      .when(Boolean(w.buttons), (el) =>
        el.child(
          h_flex()
            .flex_none()
            .pl(6)
            .gap(8)
            .child(windowButton("helm-min", "win-min", "window.minimize"))
            .child(windowButton("helm-max", w.maximized ? "win-restore" : "win-max", "window.maximize"))
            .child(windowButton("helm-close", "win-close", "window.close")),
        ),
      );
  }
}

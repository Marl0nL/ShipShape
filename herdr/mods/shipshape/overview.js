// The watch: with no ship selected, the console shows every agent's last word
// instead of one tab's terminals. A card for each hand, ship by ship; click one
// to go to that agent (which selects its ship again).
//
// The last word is read off each terminal's screen (`hc read`): the last thing
// the agent said (its last "●" paragraph), else the last few lines.

import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { agents, hc, run, theme, layout } from "herdr";
import { FELL, WORDS, ORDER, fleet, goalOf, clean, pip, programIcon, show, find } from "./kit.js";

const EVERY = 8000;

// What the agent last said, from its screen.
function lastWord(text) {
  const lines = text.replace(/\r/g, "").split("\n").map((l) => l.replace(/\s+$/, ""));
  let at = -1;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (/^\s*●\s/.test(lines[i])) {
      at = i;
      break;
    }
  }
  if (at < 0) {
    // No message on screen: the last few lines that aren't the prompt box or its rules.
    // (Not the program's banner either: block-drawn art, the model line, the folder.)
    return lines.filter((l) => l.trim() && !/^[\s─━❯>⏵✔▏▎▍│]/.test(l) && !/[▐▛▜▌▝▘█▟▙▚▞]/.test(l) && !/^\s*(~|\/)[\w/.-]*\s*$/.test(l)).slice(-4).map((l) => l.trim()).join("\n");
  }
  const out = [lines[at].replace(/^\s*●\s/, "")];
  for (let i = at + 1; i < lines.length && out.length < 9; i++) {
    const l = lines[i];
    if (/^\s*[●❯✻✽✶*]\s/.test(l) || /^[─━]/.test(l.trim())) break;
    if (!l.trim() && (!lines[i + 1] || !lines[i + 1].trim() || /^\s*[●❯✻✽✶*─━]/.test(lines[i + 1]))) break;
    out.push(l.replace(/^ {1,2}/, ""));
  }
  return out.join("\n").trim();
}

export default class Overview {
  init(_p, cx) {
    this.said = {}; // terminal address -> its last word
    const read = (cx) =>
      cx.spawn(async (cx) => {
        // Only while it's the console's shown side.
        if (find(layout(), "console")?.active !== "overview") return;
        for (const a of agents()) {
          try {
            const r = await hc(["read", a.address, "--lines", "40"]);
            if (r.code === 0) this.said[a.address] = lastWord(r.stdout);
          } catch {
            // Unreachable just now: keep what we had.
          }
        }
        cx.notify();
      });
    this.read = read;
    read(cx);
    cx.timer.every(EVERY, read);
  }

  render(cx) {
    const t = theme();
    const shown = find(layout(), "console")?.active === "overview";
    // Just turned to: read now rather than wait for the next round.
    if (shown && !this.wasShown) cx.timer.after(50, this.read);
    this.wasShown = shown;
    const ships = fleet().filter((s) => s.crew.length);
    const rank = (a) => ORDER.indexOf(a.status);

    const card = (ship, a) => {
      const said = this.said[a.address];
      return v_flex()
        .id(`word-${a.address}`)
        .flex_1()
        .min_w(300)
        .max_w(460)
        .h(178)
        .p(11)
        .gap(6)
        .rounded(9)
        .bg(t.surface)
        .border_1()
        .border_color(a.status === "blocked" ? t.blocked : a.status === "done" ? t.done : t.border)
        .cursor_pointer()
        .hover((s) => s.bg(t.raised))
        .tooltip("Go to this agent")
        .on_click(() => {
          show(ship.id);
          run("pane.focus", { pane: a.pane });
        })
        .child(
          h_flex()
            .flex_none()
            .gap(7)
            .items_center()
            .child(pip(t, a.status, 7))
            .child(programIcon(a.program, 13, t.muted) ?? div())
            .child(div().flex_1().min_w(0).truncate().font_semibold().text_size(12.5).text_color(t.foreground).child(goalOf(a) ?? (clean(a.title) || a.tab_name || "a hand")))
            .child(div().flex_none().text_size(10.5).text_color(a.status === "idle" ? t.dim : t[a.status]).child(WORDS[a.status])),
        )
        .child(
          div()
            .flex_1()
            .min_h(0)
            .overflow_hidden()
            .whitespace_normal()
            .text_size(12)
            .text_color(said ? t.muted : t.dim)
            .child(said === undefined ? "Reading…" : said || "Nothing said yet."),
        );
    };

    return v_flex()
      .id("watch")
      .size_full()
      .bg(t.background)
      .child(
        h_flex()
          .flex_none()
          .h(44)
          .px(18)
          .gap(12)
          .items_center()
          .border_b_1()
          .border_color(t.border)
          .child(div().font_family(FELL).italic().text_size(19).text_color(t.foreground).child("The Watch"))
          .child(div().flex_1().text_size(11.5).text_color(t.dim).child("No ship selected: every hand's last word. Click a card, or a ship on the chart, to go to it.")),
      )
      .child(
        v_flex()
          .id("watch-list")
          .flex_1()
          .min_h(0)
          .p(16)
          .gap(16)
          .overflow_y_scrollbar()
          .when(!ships.length, (el) => el.child(div().text_size(12.5).text_color(t.dim).child("No hands aboard any ship. Start an agent with + at the helm bar.")))
          .children(
            ships.map((ship) =>
              v_flex()
                .flex_none()
                .gap(8)
                .child(
                  h_flex()
                    .gap(8)
                    .items_center()
                    .child(div().font_family(FELL).italic().text_size(15.5).text_color(t.accent).child(ship.name))
                    .child(div().text_size(10.5).text_color(t.dim).child(`${ship.crew.length} ${ship.crew.length === 1 ? "hand" : "hands"}${ship.machine ? ` · ${ship.machine}` : ""}`)),
                )
                .child(h_flex().flex_wrap().gap(10).items_start().children([...ship.crew].sort((a, b) => rank(a) - rank(b)).map((a) => card(ship, a)))),
            ),
          ),
      );
  }
}

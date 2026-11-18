// The Dashboard: one tab with every agent on every machine Herdr reaches, grouped by what they
// need (Needs you, In progress, Idle), then Recent and Machines. It is the desk mod's
// `dashboard` part, opened from the bar's Dashboard button or `desk.dashboard`:
//
//   herdr-app do tab.new '{"name":"Dashboard","mod":"desk","props":{"part":"dashboard"}}'
//
// Everything on it is Herdr's live state (dash.js): it reads no file and runs nothing.
// A row's Go focuses that agent; Peek shows its terminal over the Dashboard (terminal.peek).

import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { agents, spaces, panes, terminals, machines, shared, run } from "herdr";
import { agentName } from "./model.js";
import { liveRows, sections, machineSummary, filterChoices, LastSeen, Transitions, span, machineKey } from "./dash.js";
import { label, pip, icon, button } from "./ui.js";

// One history and one memory of unreachable machines for every copy of the mod. The desk header
// feeds the history too (watch()), so Recent starts when Herdr does, not when the tab opens.
const transitions = new Transitions(50);
const lastSeen = new LastSeen();

export function watch() {
  return transitions.feed(agents(), agentName);
}

/** What the Dashboard shows now, read from Herdr. */
export function dashboardData(filter = {}) {
  const now = Date.now();
  const live = liveRows({ agents: agents(), spaces: spaces(), panes: panes(), terminals: terminals(), index: shared("desk.tagIndex") ?? {} }, agentName);
  const ms = machines();
  const rows = lastSeen.update(live, ms);
  const summary = machineSummary(rows, ms, lastSeen);
  return {
    now,
    rows,
    sections: sections(rows, filter),
    choices: filterChoices(rows),
    machines: summary,
    recent: transitions.list.slice(0, 15),
    since: transitions.startedAt,
  };
}

const clock = (ms) => {
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};
const where = (r) => [r.machine ?? "this computer", r.spaceName].filter(Boolean).join(" · ");
const STATE_WORD = { blocked: "blocked", done: "done", working: "working", idle: "idle" };

function go(r) {
  try {
    run("pane.focus", { pane: r.pane });
  } catch {}
}
function peek(r) {
  try {
    run("terminal.peek", { terminal: r.address });
  } catch {}
}

// Go and Peek, or why not. A crew is typed into directly when peeked: its tooltip says so.
function actions(t, L, r, idp) {
  const off = r.reachable === false ? `${r.machine ?? "This computer"} is unreachable` : null;
  const crew = r.kind === "crew";
  return h_flex()
    .flex_none()
    .gap(4)
    .items_center()
    .child(button(t, L, `${idp}-peek`, "Peek", () => peek(r), { h: 22, off, tip: crew ? `Peek at ${r.name} over the Dashboard. Careful: this is the crew's own terminal; what you type steers it directly, so talk to its manager instead` : `Peek at ${r.name} over the Dashboard; close the peek to come back here` }))
    .child(button(t, L, `${idp}-go`, "Go", () => go(r), { h: 22, off, tip: `Go to ${r.name}` }));
}

function row(t, L, d, r, depth, idp, extra) {
  const dim = r.reachable === false;
  const dur = Number.isFinite(r.since) ? `${STATE_WORD[r.status] ?? r.status} ${span(d.now - r.since)}` : STATE_WORD[r.status] ?? r.status;
  const line = extra?.line ?? r.msg ?? null;
  return h_flex()
    .id(idp)
    .w_full()
    .min_w(0)
    .gap(8)
    .items_center()
    .py(3)
    .px(6)
    .pl(6 + depth * 16)
    .rounded(Math.min(L.f.radius, 6))
    .when(!dim, (e) => e.cursor_pointer().hover((s) => s.bg(t.hover)).on_click(() => go(r)))
    .when(dim, (e) => e.opacity(0.55))
    .when(Boolean(r.color), (e) => e.child(div().w(3).h(14).flex_none().rounded_full().bg(r.color)))
    .child(pip(t, dim ? "idle" : r.status, 7))
    .child(div().flex_none().max_w(200).truncate().font_family(L.ty.labels).font_weight(600).text_size(12.5).text_color(t.foreground).child(r.name))
    .child(div().flex_none().max_w(170).truncate().text_size(11).text_color(t.dim).child(where(r)))
    .child(div().flex_none().w(84).text_size(11).text_color(r.status === "blocked" ? t.blocked : t.muted).child(dur))
    .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(t.muted).when(Boolean(line), (e) => e.tooltip(line)).child(line ?? ""))
    .when(Boolean(extra?.right), (e) => e.child(extra.right))
    .when(Boolean(r.branch), (e) => e.child(h_flex().flex_none().max_w(180).gap(4).items_center().text_size(11).text_color(t.dim).child(icon("git-branch", 11, t.dim)).child(div().truncate().child(r.branch))))
    .child(actions(t, L, r, idp));
}

function section(t, L, title, count, children, opts = {}) {
  return v_flex()
    .gap(2)
    .child(
      h_flex()
        .id(`dash-h-${title}`)
        .gap(6)
        .items_center()
        .pb(2)
        .border_b(1)
        .border_color(opts.color ?? t.border)
        .when(Boolean(opts.toggle), (e) => e.cursor_pointer().on_click((_e, cx) => { opts.toggle(); cx.notify(); }))
        .child(div().text_size(L.f.caps ? 10.5 : 12).font_family(L.ty.labels).font_weight(600).text_color(t.foreground).child(L.f.caps ? title.toUpperCase() : title))
        .child(div().text_size(11).text_color(t.dim).child(String(count)))
        .child(div().flex_1())
        .when(Boolean(opts.right), (e) => e.child(opts.right))
        .when(Boolean(opts.toggle), (e) => e.child(icon(opts.folded ? "chevron-right" : "chevron-down", 12, t.dim))),
    )
    .when(!opts.folded, (e) => e.children(children))
    .when(!opts.folded && !children.length, (e) => e.child(div().py(4).px(6).text_size(11).text_color(t.dim).child(opts.empty ?? "Nobody")));
}

function filters(t, L, d, view) {
  const f = view.dash.filter;
  const pick = (patch) => () => {
    view.dash.filter = { ...f, ...patch };
  };
  const tagChips = [["all", null], ...d.choices.tags.map((x) => [x, x])];
  const machineChips = d.choices.machines.length > 1 ? [["all", undefined], ...d.choices.machines.map((k) => [k || "this computer", k])] : [];
  return h_flex()
    .gap(6)
    .items_center()
    .flex_wrap()
    .when(d.choices.tags.length > 0, (e) =>
      e.child(div().text_size(11).text_color(t.dim).child("tag")).children(tagChips.map(([text, v]) => button(t, L, `f-tag-${text}`, text, pick({ tag: v }), { h: 22, on: (f.tag ?? null) === v }))),
    )
    .when(machineChips.length > 0, (e) =>
      e.child(div().w(8)).child(div().text_size(11).text_color(t.dim).child("machine")).children(machineChips.map(([text, v]) => button(t, L, `f-m-${text}`, text, pick({ machine: v }), { h: 22, on: f.machine === v }))),
    );
}

function recentOf(t, L, d) {
  return d.recent.map((x, i) =>
    h_flex()
      .id(`dash-r-${i}`)
      .gap(8)
      .items_center()
      .px(6)
      .py(2)
      .text_size(11.5)
      .child(div().flex_none().w(40).text_color(t.dim).child(clock(x.at)))
      .child(div().flex_none().max_w(200).truncate().font_weight(600).text_color(t.foreground).child(x.name))
      .child(div().flex_none().text_color(t.dim).child(x.text ? "" : `${x.from} →`))
      .child(x.text ? div().flex_1().min_w(0).truncate().text_color(t.muted).child(x.text) : h_flex().gap(4).items_center().child(pip(t, x.to, 6)).child(div().text_color(t.muted).child(x.to))),
  );
}

function machinesOf(t, L, d) {
  return h_flex()
    .gap(14)
    .flex_wrap()
    .items_center()
    .px(6)
    .children(
      d.machines.map((m) => {
        const down = m.state === "unavailable";
        const color = down ? t.blocked : m.state === "loading" ? t.dim : t.done;
        const text = down ? `unreachable${m.downSince ? ` since ${clock(m.downSince)}` : ""}` : m.state === "loading" ? "connecting" : `${m.agents} agent${m.agents === 1 ? "" : "s"}${m.blocked ? `, ${m.blocked} blocked` : ""}`;
        return h_flex()
          .id(`dash-m-${m.key || "here"}`)
          .gap(5)
          .items_center()
          .text_size(11.5)
          .child(div().size(7).flex_none().rounded(4).bg(color))
          .child(div().font_weight(600).text_color(t.foreground).child(m.label))
          .child(div().text_color(down ? t.blocked : t.muted).child(text))
          .when(Boolean(m.fleet), (e) => e.child(div().max_w(420).truncate().text_color(t.dim).tooltip(m.fleet).child(`— ${m.fleet}`)));
      }),
    );
}

/** The Dashboard tab. `view.dash`: {filter, open}, kept by the desk view across renders. */
export function dashboardView(t, L, d, view, more = {}) {
  const s = d.sections;
  const open = view.dash.open;
  const live = d.rows.filter((r) => r.reachable !== false);
  const nMachines = new Set(live.map((r) => machineKey(r.machine))).size;
  const facts = [`${live.length} agent${live.length === 1 ? "" : "s"} on ${nMachines} machine${nMachines === 1 ? "" : "s"}`, ...(more.facts ?? [])];
  const needs = [...s.needs.map((r, i) => row(t, L, d, r, 0, `dash-n-${i}`, more.rowExtra?.(r))), ...(more.needsMore ?? [])];
  return v_flex()
    .id("dashboard")
    .size_full()
    .min_h(0)
    .overflow_y_scrollbar()
    .p(L.d.pad + 6)
    .gap(L.d.gap + 8)
    .child(
      h_flex()
        .gap(10)
        .items_center()
        .child(div().text_size(17).font_family(L.ty.titles).font_weight(600).text_color(t.foreground).child("Dashboard"))
        .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(t.dim).child(facts.join(" · ")))
        .when(Boolean(more.pill), (e) => e.child(more.pill)),
    )
    .child(filters(t, L, d, view))
    .child(section(t, L, "Needs you", needs.length, needs, { color: needs.length ? t.blocked : t.border, empty: "Nobody is waiting on you" }))
    .child(section(t, L, "In progress", s.working.length, s.working.map(({ row: r, depth }, i) => row(t, L, d, r, depth, `dash-w-${i}`, more.rowExtra?.(r))), { color: t.working, empty: "Nobody is working" }))
    .child(
      section(t, L, "Idle", s.idle.length, s.idle.map(({ row: r, depth }, i) => row(t, L, d, r, depth, `dash-i-${i}`, more.rowExtra?.(r))), {
        folded: !open.idle,
        toggle: () => {
          open.idle = !open.idle;
        },
      }),
    )
    .when(s.unreachable.length > 0, (e) =>
      e.child(section(t, L, "Unreachable", s.unreachable.length, s.unreachable.map(({ row: r, depth }, i) => row(t, L, d, r, depth, `dash-u-${i}`, { line: `last seen ${span(d.now - (r.seenAt ?? d.now))} ago${r.msg ? `: ${r.msg}` : ""}`, ...(more.rowExtra?.(r) ?? {}) })), { color: t.blocked })),
    )
    .child(section(t, L, `Recent${d.since ? ` (since ${clock(d.since)})` : ""}`, d.recent.length, recentOf(t, L, d), { empty: "Nothing has changed since Herdr started" }))
    .child(v_flex().gap(4).child(label(t, L, "Machines")).child(machinesOf(t, L, d)));
}

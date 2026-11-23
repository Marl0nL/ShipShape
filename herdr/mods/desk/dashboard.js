// The Dashboard: one tab with every agent on every machine Herdr reaches, grouped by what they
// need (Needs you, In progress, Idle), then Recent and Machines. It is the desk mod's
// `dashboard` part, opened from the bar's Dashboard button or `desk.dashboard`:
//
//   herdr-app do tab.new '{"name":"Dashboard","mod":"desk","props":{"part":"dashboard"}}'
//
// Step 1 is Herdr's live state (dash.js). Step 2 adds what agents keep: every board, read in one
// batch per machine every 15 s while the Dashboard is on show (BoardReader; never while hidden,
// never on a machine Herdr does not report ready), and the Fleet panel's last read of
// FleetLedger (shared fleet.summary, which it publishes; the Dashboard asks FleetLedger nothing).
// A row's Go focuses that agent; Peek shows its terminal over the Dashboard (terminal.peek).

import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { agents, spaces, panes, terminals, machines, shared, run, exec } from "herdr";
import { agentName, fmHomeOf } from "./model.js";
import { liveRows, sections, machineSummary, filterChoices, LastSeen, Transitions, BoardReader, blockedTasks, boardRecent, fleetView, signinMark, span, machineKey } from "./dash.js";
import { openPlan } from "./fleet-model.js";
import { label, pip, icon, button } from "./ui.js";

// One history and one memory of unreachable machines for every copy of the mod. The desk header
// feeds the history too (watch()), so Recent starts when Herdr does, not when the tab opens.
const transitions = new Transitions(50);
const lastSeen = new LastSeen();
const boards = new BoardReader({ exec });
const homeOf = (r) => (r.task ? fmHomeOf(r.address) : null);

/** Step 2's reads, from the desk view's tick while the Dashboard is on show. True: redraw. */
export async function readBoards() {
  try {
    const rows = liveRows({ agents: agents(), spaces: spaces(), panes: panes(), terminals: terminals(), index: shared("desk.tagIndex") ?? {} }, agentName);
    return await boards.tick(rows, machines(), homeOf);
  } catch {
    return false;
  }
}

export function watch() {
  return transitions.feed(agents(), agentName);
}

/** What the Dashboard shows now, read from Herdr. */
export function dashboardData(filter = {}) {
  const now = Date.now();
  const live = liveRows({ agents: agents(), spaces: spaces(), panes: panes(), terminals: terminals(), index: shared("desk.tagIndex") ?? {} }, agentName);
  const ms = machines();
  const rows = lastSeen.update(live, ms);
  const fleet = shared("fleet.summary") ?? null;
  const summary = machineSummary(rows, ms, lastSeen).map((m) => ({ ...m, signin: signinMark(fleet, m.name, now), ...(m.state === "unavailable" && m.name ? { fleet: fleetView(fleet, m.name, now) } : {}) }));
  const boardOf = (r) => boards.boardOf(r);
  const crewOf = (r) => (r.task ? boards.crewOf(r, homeOf(r)) : null);
  const names = new Map(rows.map((r) => [r.address, r.name]));
  const recent = [...transitions.list, ...boardRecent(rows, boardOf, crewOf)].sort((a, b) => b.at - a.at).slice(0, 15);
  return {
    now,
    rows,
    sections: sections(rows, filter),
    choices: filterChoices(rows),
    machines: summary,
    recent,
    since: transitions.startedAt,
    boardOf,
    crewOf,
    nameOf: (address) => (address ? names.get(address) ?? null : null),
    tasks: blockedTasks(rows, boardOf).filter(({ row }) => applyOne(row, filter)),
    fleetOnly: fleetOnly(rows, summary, now).filter((r) => applyOne(r, filter)),
    fleet,
    boardsAt: boards.readAt(),
    boardErrors: boards.errors(),
  };
}

// Agents on a Ship Herdr cannot reach and has no rows for (it was down when Herdr started):
// FleetLedger's last word on them, greyed, with nothing to go to.
function fleetOnly(rows, summary, now) {
  const have = new Set(rows.map((r) => machineKey(r.machine)));
  const out = [];
  for (const m of summary) {
    if (!m.fleet || have.has(m.key)) continue;
    const ship = m.fleet;
    for (const [i, a] of (ship.agents ?? []).entries()) {
      out.push({ address: `${m.key}/fleetledger-${i}`, id: null, pane: null, machine: m.name, spaceName: "", name: a.name ?? "agent", status: a.state === "unknown" ? "idle" : a.state, since: null, branch: null, kind: null, tags: [], color: null, parent: null, msg: a.message, reachable: false, seenAt: ship.at ?? now, fromFleet: true });
    }
  }
  return out;
}

const applyOne = (r, f) => (!f.tag || (r.tags ?? []).includes(f.tag)) && (f.machine === undefined || f.machine === null || machineKey(r.machine) === f.machine);

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

const openFleet = () => openPlan(panes(), spaces()).forEach(([command, args]) => run(command, args));

// Go and Peek, or why not. A crew is typed into directly when peeked: its tooltip says so.
// A row FleetLedger alone knows of (its Ship is unreachable) offers the Fleet panel instead.
function actions(t, L, r, idp) {
  if (r.fromFleet) return button(t, L, `${idp}-fleet`, "Open in Fleet ›", openFleet, { h: 22, tip: `${r.machine} is unreachable from here: the Fleet panel has its reboot and update` });
  const off = r.reachable === false ? `${r.machine ?? "This computer"} is unreachable` : null;
  const crew = r.kind === "crew";
  return h_flex()
    .flex_none()
    .gap(4)
    .items_center()
    .child(button(t, L, `${idp}-peek`, "Peek", () => peek(r), { h: 22, off, tip: crew ? `Peek at ${r.name} over the Dashboard. Careful: this is the crew's own terminal; what you type steers it directly, so talk to its manager instead` : `Peek at ${r.name} over the Dashboard; close the peek to come back here` }))
    .child(button(t, L, `${idp}-go`, "Go", () => go(r), { h: 22, off, tip: `Go to ${r.name}` }));
}

// What a row says: a blocked agent's own message first; else its board's summary, firstmate's
// latest report for a crew without a board, or its message. Its next steps' tally on the right.
function lineOf(d, r) {
  const b = d.boardOf?.(r);
  const c = d.crewOf?.(r);
  const fromCrew = c?.last?.text ? `${c.last.verb}: ${c.last.text}` : null;
  if (r.reachable === false) return `${r.fromFleet ? "FleetLedger" : "last seen"} ${span(d.now - (r.seenAt ?? d.now))} ago${r.msg ? `: ${r.msg}` : b?.summary ? `: ${b.summary}` : ""}`;
  if (r.status === "blocked") return r.msg ?? b?.summary ?? fromCrew;
  return b?.summary ?? fromCrew ?? r.msg ?? null;
}

function row(t, L, d, r, depth, idp) {
  const dim = r.reachable === false;
  const dur = Number.isFinite(r.since) ? `${STATE_WORD[r.status] ?? r.status} ${span(d.now - r.since)}` : STATE_WORD[r.status] ?? r.status;
  const line = lineOf(d, r);
  const next = d.boardOf?.(r)?.next;
  const branch = r.branch ?? d.crewOf?.(r)?.meta?.branch ?? null;
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
    // Its manager, when that one is in another section (an idle manager's working crews).
    .when(depth === 0 && Boolean(d.nameOf?.(r.parent)), (e) => e.child(div().flex_none().max_w(140).truncate().text_size(11).text_color(t.dim).child(`of ${d.nameOf(r.parent)}`)))
    .child(div().flex_none().max_w(170).truncate().text_size(11).text_color(t.dim).child(where(r)))
    .child(div().flex_none().w(84).text_size(11).text_color(r.status === "blocked" ? t.blocked : t.muted).child(dur))
    .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(t.muted).when(Boolean(line), (e) => e.tooltip(line)).child(line ?? ""))
    .when(Boolean(next), (e) => e.child(div().flex_none().text_size(11).text_color(t.dim).when(Boolean(next.now), (x) => x.tooltip(`Next: ${next.now}`)).child(`next ${next.done}/${next.total}`)))
    .when(Boolean(branch), (e) => e.child(h_flex().flex_none().max_w(180).gap(4).items_center().text_size(11).text_color(t.dim).child(icon("git-branch", 11, t.dim)).child(div().truncate().child(branch))))
    .child(actions(t, L, r, idp));
}

// A manager's blocked task, in Needs you even while the manager is idle: Go goes to the manager.
function taskRow(t, L, d, { row: r, task }, idp) {
  const text = `task ${task.title ?? task.id} blocked${task.note ? `: «${task.note}»` : ""}`;
  return h_flex()
    .id(idp)
    .w_full()
    .min_w(0)
    .gap(8)
    .items_center()
    .py(3)
    .px(6)
    .rounded(Math.min(L.f.radius, 6))
    .cursor_pointer()
    .hover((s) => s.bg(t.hover))
    .on_click(() => go(r))
    .child(div().w(7).flex_none().text_size(12).font_weight(700).text_color(t.blocked).child("!"))
    .child(div().flex_none().max_w(200).truncate().font_family(L.ty.labels).font_weight(600).text_size(12.5).text_color(t.foreground).child(r.name))
    .child(div().flex_none().max_w(170).truncate().text_size(11).text_color(t.dim).child(where(r)))
    .child(div().flex_none().w(84).text_size(11).text_color(t.blocked).child(task.at ? `blocked ${span(d.now - task.at)}` : "blocked"))
    .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(t.muted).tooltip(text).child(text))
    .when(Boolean(task.agent), (e) => e.child(div().flex_none().max_w(140).truncate().text_size(11).text_color(t.dim).child(`on ${task.agent}`)))
    .child(button(t, L, `${idp}-go`, "Go", () => go(r), { h: 22, tip: `Go to ${r.name}, whose board has this task` }));
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
  // A chosen tag or machine stays offered (and "all" with it) after its last agent has gone,
  // so a filter can always be cleared.
  const tags = [...new Set([...d.choices.tags, ...(f.tag ? [f.tag] : [])])];
  const ms = [...new Set([...d.choices.machines, ...(f.machine !== undefined && f.machine !== null ? [f.machine] : [])])];
  const tagChips = [["all", null], ...tags.map((x) => [x, x])];
  const machineChips = ms.length > 1 || (f.machine !== undefined && f.machine !== null) ? [["all", undefined], ...ms.map((k) => [k || "this computer", k])] : [];
  return h_flex()
    .gap(6)
    .items_center()
    .flex_wrap()
    .when(tags.length > 0, (e) =>
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
      .when(!x.text, (e) => e.child(div().flex_none().text_color(t.dim).child(`${x.from} →`)).child(h_flex().gap(4).items_center().child(pip(t, x.to, 6)).child(div().text_color(t.muted).child(x.to))))
      .when(Boolean(x.text), (e) => e.child(div().flex_1().min_w(0).truncate().text_color(x.tone === "bad" ? t.blocked : x.tone === "warn" ? t.ansi3 ?? t.working : t.muted).tooltip(x.text).child(x.text))),
  );
}

// A Ship's sign-ins that need you, beside it in Machines (shared fleet.summary): amber, and it
// opens the Fleet panel, where its Sign in is.
function signinChip(t, L, id, mark) {
  const warn = t.ansi3 ?? t.working;
  return h_flex()
    .id(id)
    .flex_none()
    .h(20)
    .px(7)
    .items_center()
    .rounded(Math.min(L.f.radius, 8))
    .border_1()
    .border_color(`${warn}88`)
    .bg(`${warn}1f`)
    .text_size(11)
    .text_color(warn)
    .cursor_pointer()
    .hover((s) => s.bg(t.hover))
    .tooltip(mark.tip)
    .on_click((_e, cx) => {
      cx.stop_propagation();
      openFleet();
      cx.notify();
    })
    .child(`${mark.text} ›`);
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
          .when(Boolean(m.signin), (e) => e.child(signinChip(t, L, `dash-m-${m.key}-signin`, m.signin)))
          .when(Boolean(m.fleet), (e) => e.child(div().max_w(420).truncate().text_color(t.dim).tooltip(m.fleet.line).child(`— ${m.fleet.line}`)))
          .when(down && Boolean(m.name), (e) => e.child(button(t, L, `dash-m-${m.key}-fleet`, "Open in Fleet ›", openFleet, { h: 20, tip: `The Fleet panel: ${m.name}'s reboot and update` })));
      }),
    );
}

// "Fleet: 2 need you ›", from the Fleet panel's last read (shared fleet.summary): it opens the
// Fleet tab, and repeats none of FleetLedger's items.
function fleetPill(t, L, d) {
  const f = d.fleet;
  if (!f) return null;
  const age = f.readAt ? ` (read ${span(d.now - f.readAt)} ago)` : "";
  const text = f.needs ? `Fleet: ${f.needs} need${f.needs === 1 ? "s" : ""} you ›` : "Fleet ›";
  return button(t, L, "dash-fleet", text, openFleet, { h: 24, on: f.high > 0, tip: `Open the Fleet panel${age}` });
}

/** The Dashboard tab. `view.dash`: {filter, open}, kept by the desk view across renders. */
export function dashboardView(t, L, d, view) {
  const s = d.sections;
  const open = view.dash.open;
  const live = d.rows.filter((r) => r.reachable !== false);
  const nMachines = new Set(live.map((r) => machineKey(r.machine))).size;
  const facts = [`${live.length} agent${live.length === 1 ? "" : "s"} on ${nMachines} machine${nMachines === 1 ? "" : "s"}`];
  if (d.boardsAt) facts.push(`boards read ${span(d.now - d.boardsAt)} ago`);
  const fleetBlocked = (d.fleetOnly ?? []).filter((r) => r.status === "blocked");
  const needs = [
    ...s.needs.map((r, i) => row(t, L, d, r, 0, `dash-n-${i}`)),
    ...(d.tasks ?? []).map((x, i) => taskRow(t, L, d, x, `dash-t-${i}`)),
    ...fleetBlocked.map((r, i) => row(t, L, d, r, 0, `dash-nf-${i}`)),
  ];
  const away = [...s.unreachable, ...(d.fleetOnly ?? []).map((r) => ({ row: r, depth: 0 }))];
  const pill = fleetPill(t, L, d);
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
        .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(t.dim).when((d.boardErrors ?? []).length > 0, (e) => e.tooltip(`Boards not read: ${d.boardErrors.join("; ")}`)).child(facts.join(" · ")))
        .when(Boolean(pill), (e) => e.child(pill)),
    )
    .child(filters(t, L, d, view))
    .child(section(t, L, "Needs you", needs.length, needs, { color: needs.length ? t.blocked : t.border, empty: "Nobody is waiting on you" }))
    .child(section(t, L, "In progress", s.working.length, s.working.map(({ row: r, depth }, i) => row(t, L, d, r, depth, `dash-w-${i}`)), { color: t.working, empty: "Nobody is working" }))
    .child(
      section(t, L, "Idle", s.idle.length, s.idle.map(({ row: r, depth }, i) => row(t, L, d, r, depth, `dash-i-${i}`)), {
        folded: !open.idle,
        toggle: () => {
          open.idle = !open.idle;
        },
      }),
    )
    .when(away.length > 0, (e) => e.child(section(t, L, "Unreachable", away.length, away.map(({ row: r, depth }, i) => row(t, L, d, r, depth, `dash-u-${i}`)), { color: t.blocked })))
    .child(section(t, L, `Recent${d.since ? ` (since ${clock(d.since)})` : ""}`, d.recent.length, recentOf(t, L, d), { empty: "Nothing has changed since Herdr started" }))
    .child(v_flex().gap(4).child(label(t, L, "Machines")).child(machinesOf(t, L, d)));
}

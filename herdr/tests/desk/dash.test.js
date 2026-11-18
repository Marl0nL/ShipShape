// The Dashboard's logic (herdr/mods/desk/dash.js) and its view (dashboard.js), drawn with the
// stand-ins for gpui and Herdr (stand-in/). All names are placeholders.

import { test } from "node:test";
import assert from "node:assert/strict";

import { liveRows, reportText, oneLine, sinceOf, span, LastSeen, Transitions, needsOrder, nestByMachine, sections, filterChoices, applyFilter, machineSummary, dashboardPlan, dashboardShown } from "../../mods/desk/dash.js";
import * as herdr from "herdr";
import { texts, walk } from "gpui-kit";
import { resolve } from "../../mods/desk/looks.js";
import { fleet, NOW, MIN } from "./fixtures.js";


test("oneLine and reportText: agent-written text on one line, from whatever report carries", () => {
  assert.equal(oneLine("  waiting\n\tfor\u0007 you "), "waiting for you");
  assert.equal(oneLine("x".repeat(300), 10), `${"x".repeat(9)}…`);
  assert.equal(oneLine(""), null);
  assert.equal(oneLine(42), null);
  assert.equal(reportText({ report: "running tests" }), "running tests");
  assert.equal(reportText({ report: { msg: "permission: rm" } }), "permission: rm");
  assert.equal(reportText({ report: { status: { msg: "question" } } }), "question");
  assert.equal(reportText({ report: { state: "working" } }), null); // nothing it can use: left out
  assert.equal(reportText({ status: { msg: "from hc get" } }), "from hc get");
  assert.equal(reportText({ report: null, status: "working" }), null);
  assert.equal(reportText(undefined), null);
});

test("sinceOf and span: working_since while working, else the tab's last change", () => {
  assert.equal(sinceOf("working", { working_since: 5, changed_at: 9 }), 5);
  assert.equal(sinceOf("working", { working_since: null, changed_at: 9 }), 9);
  assert.equal(sinceOf("blocked", { working_since: 5, changed_at: 9 }), 9);
  assert.equal(sinceOf("idle", { changed_at: null }), null);
  assert.equal(sinceOf("idle", undefined), null);
  assert.deepEqual([span(30_000), span(34 * MIN), span(3 * 60 * MIN), span(72 * 60 * MIN), span(NaN)], ["30s", "34m", "3h", "3d", ""]);
});

test("liveRows: one row per agent, everything from Herdr's live state", () => {
  const f = fleet();
  const rows = liveRows(f);
  assert.equal(rows.length, 5);
  const crew = rows.find((r) => r.address === "t2");
  assert.deepEqual(
    { machine: crew.machine, name: crew.name, kind: crew.kind, status: crew.status, since: crew.since, branch: crew.branch, parent: crew.parent, msg: crew.msg, tags: crew.tags },
    { machine: null, name: "fm-fix-login", kind: "crew", status: "blocked", since: NOW - 6 * MIN, branch: "fix/login", parent: "t1", msg: "permission: npm install", tags: ["work"] },
  );
  const coder = rows.find((r) => r.address === "ship-01/t7");
  assert.equal(coder.machine, "ship-01");
  assert.equal(coder.since, NOW - 34 * MIN);
  assert.equal(coder.kind, "coder");
  assert.equal(coder.msg, "running pytest -k cursor");
  assert.equal(coder.id, "t7");
  assert.deepEqual(rows.find((r) => r.address === "ship-02/t3").tags, ["lab"]); // its own label, no index entry
  assert.equal(rows.find((r) => r.address === "t1").file, "/home/agent/.local/state/herdr-desk/release-lead.json");
});

test("needsOrder: the longest blocked first, then the latest done", () => {
  const rows = [
    { address: "a", status: "blocked", since: NOW - 2 * MIN },
    { address: "b", status: "done", since: NOW - 9 * MIN },
    { address: "c", status: "blocked", since: NOW - 20 * MIN },
    { address: "d", status: "blocked", since: null },
    { address: "e", status: "done", since: NOW - MIN },
    { address: "f", status: "working", since: NOW },
  ];
  assert.deepEqual(needsOrder(rows).map((r) => r.address), ["c", "a", "d", "e", "b"]);
});

test("nestByMachine: this computer first, crews under their manager, a loop shown once", () => {
  const r = (address, machine, name, parent = null) => ({ address, machine, name, parent, spaceName: "" });
  const out = nestByMachine([r("ship-01/t7", "ship-01", "b"), r("t2", null, "crew", "t1"), r("t1", null, "lead"), r("ship-01/t8", "ship-01", "a"), r("x", null, "x", "y"), r("y", null, "y", "x")]);
  assert.deepEqual(out.map((o) => [o.row.address, o.depth]), [["t1", 0], ["t2", 1], ["x", 0], ["y", 1], ["ship-01/t8", 0], ["ship-01/t7", 0]]);
});

test("sections, filters and the Machines line", () => {
  const f = fleet();
  const rows = liveRows(f);
  const s = sections(rows);
  assert.deepEqual(s.needs.map((r) => r.address), ["ship-01/t8", "t2", "ship-02/t3"]);
  assert.deepEqual(s.working.map((o) => o.row.address), ["ship-01/t7"]);
  assert.deepEqual(s.idle.map((o) => o.row.address), ["t1"]);
  assert.deepEqual(filterChoices(rows), { tags: ["lab", "work"], machines: ["", "ship-01", "ship-02"] });
  assert.deepEqual(applyFilter(rows, { tag: "work" }).map((r) => r.address), ["t1", "t2"]);
  assert.deepEqual(applyFilter(rows, { machine: "ship-01" }).map((r) => r.address), ["ship-01/t7", "ship-01/t8"]);
  assert.deepEqual(applyFilter(rows, { machine: "" }).map((r) => r.address), ["t1", "t2"]);
  const m = machineSummary(rows, [...f.machines, { name: "ship-03", layout: "loading" }]);
  assert.deepEqual(m.map((x) => [x.label, x.state, x.agents, x.blocked]), [["this computer", "ready", 2, 1], ["ship-01", "ready", 2, 1], ["ship-02", "ready", 1, 0], ["ship-03", "loading", 0, 0]]);
});

test("LastSeen: an unreachable machine's agents stay, greyed and dated, whichever way Herdr goes", () => {
  let t = NOW;
  const seen = new LastSeen(() => t);
  const f = fleet();
  const rows = liveRows(f);
  assert.equal(seen.update(rows, f.machines).filter((r) => r.reachable === false).length, 0);
  t += 5 * MIN;
  const down = f.machines.map((m) => (m.name === "ship-01" ? { ...m, layout: "unavailable" } : m));
  // Herdr keeps listing them: marked unreachable.
  const kept = seen.update(rows, down);
  assert.deepEqual(kept.filter((r) => r.reachable === false).map((r) => r.address), ["ship-01/t7", "ship-01/t8"]);
  assert.equal(seen.downSince("ship-01"), t);
  // Herdr drops them: the last ones seen stand in, dated when they were seen.
  t += 5 * MIN;
  const dropped = seen.update(rows.filter((r) => r.machine !== "ship-01"), down);
  const stand = dropped.filter((r) => r.reachable === false);
  assert.deepEqual(stand.map((r) => r.address), ["ship-01/t7", "ship-01/t8"]);
  assert.equal(stand[0].seenAt, NOW);
  assert.equal(stand[0].downSince, NOW + 5 * MIN);
  assert.deepEqual(sections(dropped).unreachable.map((o) => o.row.address), ["ship-01/t8", "ship-01/t7"].sort());
  assert.ok(!sections(dropped).needs.some((r) => r.machine === "ship-01"));
  // Back: live again, and nothing stands in.
  t += MIN;
  assert.equal(seen.update(rows, f.machines).filter((r) => r.reachable === false).length, 0);
  assert.equal(seen.downSince("ship-01"), null);
});

test("Transitions: every status change once, newest first, kept to its limit", () => {
  let t = NOW;
  const tr = new Transitions(3, () => t);
  const a = (address, status) => ({ address, status, tab_name: address });
  assert.equal(tr.feed([a("t1", "idle"), a("t2", "working")]), false); // the start: nothing moved
  assert.equal(tr.startedAt, NOW);
  assert.equal(tr.feed([a("t1", "idle"), a("t2", "working")]), false);
  t += MIN;
  assert.equal(tr.feed([a("t1", "working"), a("t2", "blocked")]), true);
  t += MIN;
  tr.feed([a("t1", "done"), a("t2", "blocked"), a("t3", "working")]); // a new agent is no change
  assert.deepEqual(tr.list.map((x) => [x.address, x.from, x.to]), [["t1", "working", "done"], ["t2", "working", "blocked"], ["t1", "idle", "working"]]);
  t += MIN;
  tr.feed([a("t1", "idle"), a("t2", "blocked"), a("t3", "working")]);
  assert.equal(tr.list.length, 3);
  assert.deepEqual(tr.list[0], { at: t, address: "t1", name: "t1", from: "done", to: "idle" });
});

test("dashboardPlan: the Dashboard tab if open, else a new one in a space on this computer", () => {
  const spaces = [
    { id: "ship-01/s1", machine: "ship-01", active: true, tabs: [{ id: "ship-01/tb1", name: "1", active: true }] },
    { id: "s2", machine: null, active: false, tabs: [{ id: "tb5", name: "Dashboard", active: false }, { id: "tb6", name: "2" }] },
  ];
  assert.deepEqual(dashboardPlan([{ pane: "p9", mod: "desk", terminal: null, tab: "tb5" }], spaces), [["space.select", { id: "s2" }], ["tab.select", { id: "tb5" }]]);
  assert.deepEqual(dashboardPlan([], spaces), [["tab.new", { name: "Dashboard", space: "s2", mod: "desk", props: { part: "dashboard" } }]]);
  assert.deepEqual(dashboardPlan([], [spaces[0]]), [["tab.new", { name: "Dashboard", mod: "desk", props: { part: "dashboard" } }]]);
  // Shown only while its tab is the shown one of the shown space.
  const panes = [{ pane: "p9", mod: "desk", terminal: null, tab: "tb5" }];
  assert.equal(dashboardShown(panes, spaces), false);
  assert.equal(dashboardShown(panes, [{ ...spaces[1], active: true, tabs: [{ id: "tb5", active: true }] }]), true);
  assert.equal(dashboardShown([], spaces), true);
});

test("the Dashboard draws every section from a stand-in Herdr, and Go and Peek run Herdr's commands", async () => {
  const f = fleet();
  herdr.reset({ agents: f.agents, spaces: f.spaces, panes: f.panes, terminals: f.terminals, machines: f.machines, shared: { "desk.tagIndex": f.index } });
  const { dashboardData, dashboardView, watch } = await import("../../mods/desk/dashboard.js");
  watch();
  const view = { dash: { filter: {}, open: { idle: true } } };
  const L = resolve(null);
  const tree = dashboardView(herdr.theme(), L, dashboardData(view.dash.filter), view);
  const words = texts(tree).join(" | ");
  for (const want of ["Dashboard", "5 agents on 3 machines", "Needs you", "In progress", "Idle", "Recent", "Machines", "fm-fix-login", "permission: npm install", "feat/search", "ship-01"]) assert.ok(words.includes(want), want);
  const go = walk(tree).find((e) => e.elId === "dash-n-1-go");
  go.click(null, { notify() {}, stop_propagation() {} });
  assert.deepEqual(herdr.state.runs.at(-1), ["pane.focus", { pane: "p2" }]);
  const peek = walk(tree).find((e) => e.elId === "dash-n-1-peek");
  const click = { stopped: false, notify() {}, stop_propagation() { this.stopped = true; } };
  peek.click(null, click);
  assert.deepEqual(herdr.state.runs.at(-1), ["terminal.peek", { terminal: "t2" }]);
  assert.equal(click.stopped, true); // Peek is not also a click on the row (which would Go)
  assert.match(peek.tip, /crew's own terminal/);

  // ship-01 goes: its rows are greyed, and Go and Peek say why rather than act.
  herdr.state.machines = f.machines.map((m) => (m.name === "ship-01" ? { ...m, layout: "unavailable" } : m));
  herdr.state.agents = f.agents.filter((a) => !a.address.startsWith("ship-01/"));
  const later = dashboardView(herdr.theme(), L, dashboardData({}), view);
  assert.ok(texts(later).join(" ").includes("Unreachable"));
  const off = walk(later).find((e) => e.elId === "dash-u-0-go");
  assert.equal(off.click, null);
  assert.equal(off.tip, "ship-01 is unreachable");
});

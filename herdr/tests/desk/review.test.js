// Cases from the PR's independent review: a filter that can always be cleared, line breaks kept
// in linked text, last-seen rows kept when Herdr drops agents before it marks a machine down, a
// crew's manager named when it sits in another section, and board links opened only as https.

import { test } from "node:test";
import assert from "node:assert/strict";

import { LastSeen, EMPTY_MS, liveRows } from "../../mods/desk/dash.js";
import * as herdr from "herdr";
import { texts, walk } from "gpui-kit";
import { resolve } from "../../mods/desk/looks.js";
import { fleet, NOW, MIN } from "./fixtures.js";

const cx = { notify() {}, stop_propagation() {} };

test("LastSeen keeps a machine's rows when its agents go a moment before Herdr calls it unavailable", () => {
  let t = NOW;
  const seen = new LastSeen(() => t);
  const f = fleet();
  const rows = liveRows(f);
  seen.update(rows, f.machines);
  const without = rows.filter((r) => r.machine !== "ship-01");
  t += 2000;
  seen.update(without, f.machines); // agents gone, machine still "ready"
  t += 2000;
  const down = f.machines.map((m) => (m.name === "ship-01" ? { ...m, layout: "unavailable" } : m));
  assert.deepEqual(seen.update(without, down).filter((r) => r.reachable === false).map((r) => r.address), ["ship-01/t7", "ship-01/t8"]);
  // Ready and empty for EMPTY_MS: forgotten, so nothing stands in when it goes later.
  const later = new LastSeen(() => t);
  later.update(rows, f.machines);
  later.update(without, f.machines);
  t += EMPTY_MS;
  later.update(without, f.machines);
  assert.equal(later.update(without, down).filter((r) => r.reachable === false).length, 0);
});

test("the Dashboard: a chosen filter stays clearable, and a crew names its manager from another section", async () => {
  const f = fleet();
  herdr.reset({ agents: f.agents, spaces: f.spaces, panes: f.panes, terminals: f.terminals, machines: f.machines, shared: { "desk.tagIndex": f.index } });
  const { dashboardData, dashboardView } = await import("../../mods/desk/dashboard.js");
  const view = { dash: { filter: { tag: "lab", machine: "ship-09" }, open: { idle: true } } };
  const tree = dashboardView(herdr.theme(), resolve(null), dashboardData(view.dash.filter), view);
  walk(tree).find((e) => e.elId === "f-tag-all").click(null, cx);
  assert.equal(view.dash.filter.tag, null);
  walk(tree).find((e) => e.elId === "f-m-all").click(null, cx);
  assert.equal(view.dash.filter.machine, undefined);
  // fm-fix-login (blocked, in Needs you) reports to release-lead (idle): named on its row.
  const all = dashboardView(herdr.theme(), resolve(null), dashboardData({}), view);
  assert.ok(texts(walk(all).find((e) => e.elId === "dash-n-1")).includes("of release-lead"));
});

test("linked text keeps its line breaks, and board links open only as https", async () => {
  herdr.reset();
  const { assistant } = await import("../../mods/desk/views.js");
  const state = { board: { notes: "first line\nsecond https://example.com/x", links: [{ title: "Old", url: "http://example.com/old" }, { title: "Doc", url: "https://example.com/doc" }] } };
  const tree = assistant(herdr.theme(), resolve(null), { name: "assistant" }, state, 400, { kit: {}, note: null });
  const notes = walk(tree).find((e) => e.elId === "notes-text");
  const kinds = notes.kids.map((k) => (texts(k).join("") || "<br>"));
  assert.deepEqual(kinds, ["first", "line", "<br>", "second", "example.com/x"]);
  walk(tree).find((e) => e.elId === "a-link-0").click(null, cx);
  assert.equal(herdr.state.execs.length, 0); // http: shown, not opened
  walk(tree).find((e) => e.elId === "a-link-1").click(null, cx);
  assert.deepEqual(herdr.state.execs.at(-1), ["xdg-open", ["https://example.com/doc"], undefined]);
});

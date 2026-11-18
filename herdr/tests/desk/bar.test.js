// The top bar's "N need you" (herdr/mods/desk/main.js's bar, dash.js's blockedQueue): every
// blocked agent on any machine Herdr reaches, and a click that goes to the next one.

import { test } from "node:test";
import assert from "node:assert/strict";

import { blockedQueue, nextAfter } from "../../mods/desk/dash.js";
import * as herdr from "herdr";
import { texts, walk } from "gpui-kit";
import { resolve } from "../../mods/desk/looks.js";
import { fleet, NOW, MIN } from "./fixtures.js";

const cx = { notify() {}, stop_propagation() {} };

test("blockedQueue: blocked agents only, the longest blocked first, none on an unavailable machine", () => {
  const f = fleet();
  assert.deepEqual(blockedQueue(f.agents, f.spaces, f.machines).map((r) => [r.address, r.since]), [["ship-01/t8", NOW - 20 * MIN], ["t2", NOW - 6 * MIN]]);
  const down = f.machines.map((m) => (m.name === "ship-01" ? { ...m, layout: "unavailable" } : m));
  assert.deepEqual(blockedQueue(f.agents, f.spaces, down).map((r) => r.address), ["t2"]);
  assert.deepEqual(blockedQueue([], f.spaces, f.machines), []);
});

test("nextAfter: the next in the queue after the last one gone to, round again at the end", () => {
  const q = [{ address: "a" }, { address: "b" }, { address: "c" }];
  assert.equal(nextAfter(q, null).address, "a");
  assert.equal(nextAfter(q, "a").address, "b");
  assert.equal(nextAfter(q, "c").address, "a");
  assert.equal(nextAfter(q, "gone").address, "a");
  assert.equal(nextAfter([], "a"), null);
});

test("the bar shows how many need you, and each click goes to the next blocked agent", async () => {
  const f = fleet();
  herdr.reset({ agents: f.agents, spaces: f.spaces, panes: f.panes, terminals: f.terminals, machines: f.machines, layout: { id: "root", children: [] } });
  globalThis.localStorage ??= { getItem: () => null, setItem() {} };
  const { default: Desk } = await import("../../mods/desk/main.js");
  const desk = Object.create(Desk.prototype);
  const draw = () => desk.bar(herdr.theme(), resolve(null));
  let bar = draw();
  assert.ok(texts(bar).includes("2 need you"));
  let chip = walk(bar).find((e) => e.elId === "bar-needs");
  assert.match(chip.tip, /^Go to metrics, blocked/);
  chip.click(null, cx);
  assert.deepEqual(herdr.state.runs.at(-1), ["pane.focus", { pane: "ship-01/p8" }]);
  chip = walk(draw()).find((e) => e.elId === "bar-needs");
  assert.match(chip.tip, /^Go to fm-fix-login/);
  chip.click(null, cx);
  assert.deepEqual(herdr.state.runs.at(-1), ["pane.focus", { pane: "p2" }]);
  walk(draw()).find((e) => e.elId === "bar-needs").click(null, cx);
  assert.deepEqual(herdr.state.runs.at(-1), ["pane.focus", { pane: "ship-01/p8" }]);

  // The Dashboard button is there too; nobody blocked, no counter.
  assert.ok(walk(draw()).some((e) => e.elId === "bar-dashboard"));
  herdr.state.agents = f.agents.map((a) => ({ ...a, status: "idle" }));
  assert.equal(walk(draw()).find((e) => e.elId === "bar-needs"), undefined);
});

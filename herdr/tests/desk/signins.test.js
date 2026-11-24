// A Ship whose sign-ins need you (missing, expiring, expired, wrong account) is marked beside it in
// the Desk's sidebar (desk-spaces' machine header) and on its row in the Dashboard's Machines,
// from what the Fleet panel last read (shared fleet.summary, made here by the panel's own
// fleetSummary from its attention items): nothing new is asked of FleetLedger or a Ship.

import { test } from "node:test";
import assert from "node:assert/strict";

import * as herdr from "herdr";
import { v_flex, texts, walk, byId } from "gpui-kit";
import { fleetSummary } from "../../mods/desk/fleet-model.js";
import { signinMark } from "../../mods/desk/dash.js";
import { resolve } from "../../mods/desk/looks.js";
import { fleet, NOW, MIN } from "./fixtures.js";
import { readAgo } from "../../mods/desk-spaces/main.js";

globalThis.localStorage ??= { getItem: () => null, setItem() {}, removeItem() {} };

const item = (ship, code, subject, severity = "medium") => ({ ship, code, message: `${code} on ${ship}`, fix: "", severity, subject });
const ship = (name) => ({ name, tags: [], state: "running", problem: null, message: null, attention: null, more: 0, agents: null, statusAt: null });

/** The summary the Fleet panel publishes: ship-01 has an expired and a missing sign-in, ship-02 none. */
function summary(readAt = NOW - 4 * MIN) {
  return fleetSummary({
    shipsAt: readAt,
    shipsFrom: "FleetLedger",
    ships: [ship("ship-01"), ship("ship-02")],
    attention: [item("ship-01", "signin-expired", "claude", "high"), item("ship-01", "signin-missing", "github"), item("ship-02", "reboot-required", null), item(null, "vault-locked", "vault", "high")],
  });
}

test("signinMark: what the Dashboard says beside a Ship, from the Fleet panel's summary", () => {
  const s = summary();
  assert.deepEqual(signinMark(s, "ship-01", NOW), {
    text: "2 sign-ins",
    tip: "ship-01's sign-ins need you: claude expired, github missing (FleetLedger, read 4m ago). The Fleet panel signs it in.",
  });
  const one = fleetSummary({ shipsAt: null, shipsFrom: "FleetLedger", ships: [ship("ship-01")], attention: [item("ship-01", "signin-wrong-account", null)] });
  assert.deepEqual(signinMark(one, "ship-01", NOW), { text: "sign-in: wrong account", tip: "ship-01's sign-ins need you: wrong account. The Fleet panel signs it in." });
  assert.equal(signinMark(s, "ship-02", NOW), null); // a reboot is not a sign-in
  assert.equal(signinMark(s, "ship-09", NOW), null);
  assert.equal(signinMark(s, null, NOW), null); // this computer
  assert.equal(signinMark(null, "ship-01", NOW), null); // the Fleet panel has not read
  // An older panel's summary, with no sign-ins on its Ships: no mark.
  assert.equal(signinMark({ readAt: NOW, ships: [{ name: "ship-01", state: "running" }] }, "ship-01", NOW), null);
});

test("the Dashboard marks a Ship whose sign-ins need you in Machines, and the mark opens the Fleet panel", async () => {
  const f = fleet();
  herdr.reset({ agents: f.agents, spaces: f.spaces, panes: f.panes, terminals: f.terminals, machines: f.machines, shared: { "desk.tagIndex": f.index, "fleet.summary": summary(Date.now() - 4 * MIN) } });
  const { dashboardData, dashboardView } = await import("../../mods/desk/dashboard.js");
  const view = { dash: { filter: {}, open: { idle: false } } };
  const d = dashboardData({});
  const tree = dashboardView(herdr.theme(), resolve(null), d, view);
  const mark = byId(tree, "dash-m-ship-01-signin");
  assert.ok(mark, "ship-01 is marked");
  assert.deepEqual(texts(mark), ["2 sign-ins ›"]);
  assert.match(mark.tip, /^ship-01's sign-ins need you: claude expired, github missing \(FleetLedger, read 4m ago\)/);
  assert.equal(byId(tree, "dash-m-ship-02-signin"), null);
  assert.equal(byId(tree, "dash-m-here-signin"), null);
  mark.click(null, { notify() {}, stop_propagation() {} });
  assert.deepEqual(herdr.state.runs.at(-1), ["tab.new", { name: "Fleet", mod: "fleet" }]);
  // Nothing was asked of a Ship or of FleetLedger to draw it.
  assert.deepEqual(herdr.state.execs, []);
});

function sidebar(collapsed) {
  return import("../../mods/desk-spaces/main.js").then(({ default: Spaces }) => {
    const view = new Spaces();
    view.init({}, { notify() {}, spawn() {}, timer: { every() {} } });
    view.machines = [{ name: "ship-01", state: "connected" }, { name: "ship-02", state: "connected" }];
    return v_flex().children(view.grouped(herdr.theme(), collapsed));
  });
}

test("the sidebar marks a Ship whose sign-ins need you beside its machine header, dated", async () => {
  herdr.reset({ machines: [{ name: null, layout: "ready" }, { name: "ship-01", layout: "ready" }, { name: "ship-02", layout: "ready" }], shared: { "fleet.summary": summary(Date.now() - 4 * MIN) } });
  const open = await sidebar(false);
  const mark = byId(open, "machine-signin-ship-01");
  assert.ok(mark, "ship-01 is marked");
  assert.deepEqual(texts(mark), ["sign in"]);
  assert.equal(mark.tip, "ship-01's sign-ins need you: claude expired, github missing (FleetLedger, read 4m ago). The Fleet panel signs it in.");
  assert.equal(byId(open, "machine-signin-ship-02"), null);
  assert.ok(texts(byId(open, "machine-ship-01")).includes("ship-01"));

  // The rail: a dot on the Ship's icon, and the same words in its tooltip.
  const rail = await sidebar(true);
  assert.ok(byId(rail, "machine-signin-ship-01"));
  assert.equal(byId(rail, "machine-signin-ship-02"), null);
  assert.equal(byId(rail, "machine-ship-01").tip, "ship-01 · connected ship-01's sign-ins need you: claude expired, github missing (FleetLedger, read 4m ago). The Fleet panel signs it in.");
  assert.equal(byId(rail, "machine-ship-02").tip, "ship-02 · connected");
});

test("the sidebar draws no mark before the Fleet panel has read, or from an older panel's summary", async () => {
  for (const s of [undefined, { readAt: NOW, ships: [{ name: "ship-01", state: "running" }] }, { ships: "junk" }, { ships: [null, "junk"] }]) {
    herdr.reset({ machines: [{ name: null, layout: "ready" }, { name: "ship-01", layout: "ready" }], shared: s === undefined ? {} : { "fleet.summary": s } });
    for (const collapsed of [false, true]) {
      const tree = await sidebar(collapsed);
      assert.ok(byId(tree, "machine-ship-01"));
      assert.equal(walk(tree).filter((e) => String(e.elId ?? "").startsWith("machine-signin-")).length, 0);
    }
  }
});

test("readAgo: the sidebar dates the Fleet panel's read as the Dashboard does", () => {
  assert.equal(readAgo({ readAt: NOW - 30_000 }, NOW), " (FleetLedger, read 30s ago)");
  assert.equal(readAgo({ readAt: NOW - 4 * MIN }, NOW), " (FleetLedger, read 4m ago)");
  assert.equal(readAgo({ readAt: NOW - 3 * 60 * MIN }, NOW), " (FleetLedger, read 3h ago)");
  assert.equal(readAgo({ readAt: NOW - 72 * 60 * MIN }, NOW), " (FleetLedger, read 3d ago)");
  assert.equal(readAgo({ readAt: null }, NOW), "");
  assert.equal(readAgo(null, NOW), "");
});

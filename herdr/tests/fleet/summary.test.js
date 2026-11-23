// What the Fleet panel hands the Desk's Dashboard (herdr/mods/fleet/model.js): each Ship's last
// listed agents from its status document, and the summary it publishes as shared fleet.summary.
// The documents are the repo's own examples, from contracts/examples/, with placeholder agents.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import { readShips, shipAgents, fleetSummary, shipSignins, windowAttention, windowChange } from "../../mods/fleet/model.js";

const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const example = (kind, name) => JSON.parse(readFileSync(join(ROOT, "contracts", "examples", kind, name), "utf8"));

test("readShips keeps each Ship's agents as its last status lists them, and when that was", () => {
  const doc = example("fleetledger-ship", "answering.json");
  doc.status.herdr.agents = [
    { name: "fm-search-api", kind: "claude", state: "blocked", mode: null, pane: "t7", cwd: "/home/agent/wt", blocked_kind: "permission", message: "approve: npm install\n--save-dev" },
    { name: null, kind: "claude", state: "working", mode: null },
    { name: "odd", kind: "claude", state: "asleep", mode: null },
    "junk",
  ];
  const [ship] = readShips(JSON.stringify([doc])).ships;
  assert.deepEqual(ship.agents, [
    { name: "fm-search-api", state: "blocked", message: "approve: npm install --save-dev", blockedKind: "permission" },
    { name: null, state: "working", message: null, blockedKind: null },
  ]);
  assert.equal(ship.statusAt, Date.parse(doc.status.generated_at));
  const [none] = readShips(JSON.stringify([example("fleetledger-ship", "unreachable.json")])).ships;
  assert.equal(none.agents, null);
  assert.equal(none.statusAt, null);
  assert.equal(shipAgents(null), null);
  assert.equal(shipAgents({ herdr: { agents: "no" } }), null);
  assert.equal(shipAgents({ herdr: { agents: Array.from({ length: 150 }, () => ({ name: "a", state: "idle" })) } }).length, 100);
});

test("fleetSummary: the Ships, their agents, and how many items need you", () => {
  const state = {
    shipsAt: 1_000,
    shipsFrom: "FleetLedger",
    attention: [{ severity: "high" }, { severity: "low" }],
    ships: [{ name: "ship-01", tags: ["work"], state: "running", problem: null, message: null, attention: null, more: 0, agents: [{ name: "a", state: "idle", message: null, blockedKind: null }], statusAt: 900 }],
  };
  assert.deepEqual(fleetSummary(state), { readAt: 1_000, from: "FleetLedger", needs: 2, high: 1, ships: [{ name: "ship-01", state: "running", problem: null, statusAt: 900, agents: [{ name: "a", state: "idle", message: null, blockedKind: null }], signins: [] }] });
  assert.deepEqual(fleetSummary({ shipsAt: null, shipsFrom: null, attention: [], ships: null }), { readAt: null, from: null, needs: 0, high: 0, ships: [] });
});

// The items as readAttention keeps them: ship, code, message, fix, severity, subject.
const item = (ship, code, subject, severity = "medium") => ({ ship, code, message: `${code} on ${ship}`, fix: "", severity, subject });

test("shipSignins: a Ship's sign-in items (missing, expiring, expired, wrong account), once each, in order", () => {
  const attention = [
    item("ship-01", "signin-expired", "claude", "high"),
    item("ship-02", "signin-missing", "github"),
    item("ship-01", "reboot-required", null),
    item("ship-01", "signin-wrong-account", "github"),
    item("ship-01", "signin-expired", "claude"), // the same again: once
    item("ship-01", "signin-expiring", null), // no service named
    item("ship-01", "signin-scopes-differ", "github"), // not one a mark is for
    item(null, "vault-locked", "vault", "high"),
  ];
  assert.deepEqual(shipSignins(attention, "ship-01"), [
    { service: "claude", problem: "expired" },
    { service: "github", problem: "wrong account" },
    { service: null, problem: "expiring" },
  ]);
  assert.deepEqual(shipSignins(attention, "ship-02"), [{ service: "github", problem: "missing" }]);
  assert.deepEqual(shipSignins(attention, "ship-03"), []);
  assert.deepEqual(shipSignins(null, "ship-01"), []);
  assert.deepEqual(shipSignins([null, "junk", {}], "ship-01"), []);
});

test("fleetSummary gives each Ship its sign-ins from the items already read", () => {
  const ship = (name) => ({ name, tags: [], state: "running", problem: null, message: null, attention: null, more: 0, agents: null, statusAt: null });
  const state = { shipsAt: 1_000, shipsFrom: "FleetLedger", ships: [ship("ship-01"), ship("ship-02")], attention: [item("ship-02", "signin-missing", "claude", "high"), item(null, "vault-locked", "vault", "high")] };
  const s = fleetSummary(state);
  assert.deepEqual(s.ships.map((x) => [x.name, x.signins]), [["ship-01", []], ["ship-02", [{ service: "claude", problem: "missing" }]]]);
  assert.equal(s.needs, 2);
});

test("windowAttention and windowChange: the count in the title and badge, set only when it changes", () => {
  assert.deepEqual(windowAttention({ needs: 0 }), { title: null, badge: null });
  assert.deepEqual(windowAttention(null), { title: null, badge: null });
  assert.deepEqual(windowAttention({ needs: 1 }), { title: "Herdr · Fleet: 1 needs you", badge: "1" });
  assert.deepEqual(windowAttention({ needs: 3 }), { title: "Herdr · Fleet: 3 need you", badge: "3" });
  // Herdr starts with its own title and no badge: nothing to set while nothing needs you.
  const start = { title: null, badge: null };
  assert.equal(windowChange(start, { needs: 0 }), null);
  const two = windowChange(start, { needs: 2 });
  assert.deepEqual(two, { title: "Herdr · Fleet: 2 need you", badge: "2" });
  assert.equal(windowChange(two, { needs: 2 }), null);
  // Back to none: Herdr's own title again, and no badge.
  assert.deepEqual(windowChange(two, { needs: 0 }), { title: null, badge: null });
});

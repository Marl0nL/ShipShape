// What the Fleet panel hands the Desk's Dashboard (herdr/mods/fleet/model.js): each Ship's last
// listed agents from its status document, and the summary it publishes as shared fleet.summary.
// The documents are the repo's own examples, from contracts/examples/, with placeholder agents.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import { readShips, shipAgents, fleetSummary } from "../../mods/fleet/model.js";

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
  assert.deepEqual(fleetSummary(state), { readAt: 1_000, from: "FleetLedger", needs: 2, high: 1, ships: [{ name: "ship-01", state: "running", problem: null, statusAt: 900, agents: [{ name: "a", state: "idle", message: null, blockedKind: null }] }] });
  assert.deepEqual(fleetSummary({ shipsAt: null, shipsFrom: null, attention: [], ships: null }), { readAt: null, from: null, needs: 0, high: 0, ships: [] });
});

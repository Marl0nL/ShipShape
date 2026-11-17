// The Desk's Ship tags (herdr/mods/desk/shiptags.js), with a stand-in for Herdr. The documents it
// reads are the repo's own examples, from contracts/examples/.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import { ShipTags, FILE_MS, LEDGER_MS } from "../../mods/desk/shiptags.js";

const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const example = (kind, name) => JSON.parse(readFileSync(join(ROOT, "contracts", "examples", kind, name), "utf8"));

const HOME = "/home/agent";
const FLEET_FILE = `${HOME}/.ssh/shipshape/fleet.json`;

const bundle = (ships) => JSON.stringify({ ...example("machine-bundle", "renewed.json"), ships });
const ship = (name, tags) => ({ ...example("machine-bundle", "renewed.json").ships[0], name, tags });
const lsEntry = (name, tags) => ({ ...example("fleetledger-ship", "answering.json"), name, tags });

/** A Herdr stand-in: `files` maps a path to its text (absent: null; an Error: readTextFile
 *  throws it); `ls` is ship-fleet's answer {code, stdout, stderr}, or an Error to reject with. */
function stand({ files = {}, ls = new Error("spawn ship-fleet ENOENT") } = {}) {
  let t = 1_000_000;
  const calls = [];
  const host = {
    home: () => HOME,
    readTextFile: async (path) => {
      calls.push(["read", path]);
      const f = host.files[path];
      if (f instanceof Error) throw f;
      return f ?? null;
    },
    exec: async (program, args, opts) => {
      calls.push(["exec", program, args, opts]);
      if (host.ls instanceof Error) throw host.ls;
      return host.ls;
    },
    files,
    ls,
  };
  const tags = new ShipTags(host, () => t);
  return { host, tags, calls, tick: (ms) => (t += ms), execs: () => calls.filter((c) => c[0] === "exec") };
}

test("fleet.json gives each Ship's tags, and ship-fleet is never asked", async () => {
  const s = stand({ files: { [FLEET_FILE]: bundle([ship("ship-01", ["work"]), ship("ship-02", ["personal", "lab"])]) } });
  assert.equal(await s.tags.read(), true);
  assert.deepEqual(s.tags.get("ship-01"), ["work"]);
  assert.deepEqual(s.tags.get("ship-02"), ["personal", "lab"]);
  assert.deepEqual(s.tags.get("ship-03"), []);
  assert.deepEqual(s.tags.get(null), []);
  assert.equal(s.tags.from, "fleet.json");
  assert.equal(s.tags.problem, null);
  assert.deepEqual(s.execs(), []);
});

test("with no fleet.json, the local ship-fleet (a computer that runs FleetLedger itself)", async () => {
  const s = stand({ ls: { code: 0, stdout: JSON.stringify([lsEntry("ship-01", ["work"])]), stderr: "" } });
  assert.equal(await s.tags.read(), true);
  assert.deepEqual(s.tags.get("ship-01"), ["work"]);
  assert.equal(s.tags.from, "ship-fleet");
  assert.equal(s.tags.problem, null);
  const [[, program, args, opts]] = s.execs();
  assert.deepEqual([program, args, opts.timeout], ["ship-fleet", ["ls", "--json", "--offline"], 20000]);
});

test("with neither, no tags and a reason, not silence", async () => {
  const s = stand();
  assert.equal(await s.tags.read(), true);
  assert.equal(s.tags.from, null);
  assert.deepEqual(s.tags.get("ship-01"), []);
  assert.match(s.tags.problem, /no \/home\/agent\/\.ssh\/shipshape\/fleet\.json \(this computer has no self-refresh\); no FleetLedger on this computer \(spawn ship-fleet ENOENT\)/);

  const refused = stand({ ls: { code: 2, stdout: "", stderr: "ship-fleet: this copy is retired\nmore\n" } });
  await refused.tags.read();
  assert.match(refused.tags.problem, /ship-fleet ls failed: ship-fleet: this copy is retired$/);

  const odd = stand({ ls: { code: 0, stdout: "{not json", stderr: "" } });
  await odd.tags.read();
  assert.match(odd.tags.problem, /ship-fleet ls: not JSON/);
});

test("a malformed or hostile fleet.json is ignored, with the reason shown, and ship-fleet tried", async () => {
  const cases = [
    ["{not json", /^fleet\.json: not JSON .*; ignored; no FleetLedger on this computer/],
    [JSON.stringify([1, 2]), /^fleet\.json: not a JSON object; ignored;/],
    [JSON.stringify({ ships: [{ name: "ship-01", tags: ["work"] }] }), /^fleet\.json: no "schema" field.*; ignored;/],
    [JSON.stringify({ ...JSON.parse(bundle([])), schema: "shipshape.machine-bundle/9" }), /^fleet\.json: shape .* is newer than this panel reads.*; ignored;/],
    [JSON.stringify({ ...JSON.parse(bundle([])), schema: "evil/1" }), /^fleet\.json: unknown shape/],
  ];
  for (const [text, why] of cases) {
    const s = stand({ files: { [FLEET_FILE]: text } });
    await s.tags.read();
    assert.equal(s.tags.from, null, text);
    assert.match(s.tags.problem, why, text);
    assert.equal(s.execs().length, 1, text);
  }
  // Ignored, but the local FleetLedger answers: its tags, and still the reason.
  const s = stand({ files: { [FLEET_FILE]: "{" }, ls: { code: 0, stdout: JSON.stringify([lsEntry("ship-01", ["work"])]), stderr: "" } });
  await s.tags.read();
  assert.equal(s.tags.from, "ship-fleet");
  assert.deepEqual(s.tags.get("ship-01"), ["work"]);
  assert.match(s.tags.problem, /^fleet\.json: not JSON .*; ignored$/);
  // A file that cannot be read at all.
  const unreadable = stand({ files: { [FLEET_FILE]: new Error("permission denied\nstack") } });
  await unreadable.tags.read();
  assert.match(unreadable.tags.problem, /^Couldn't read \/home\/agent\/\.ssh\/shipshape\/fleet\.json: permission denied; ignored;/);
});

test("hostile entries in a valid fleet.json are left out; tags outside the contract's pattern too", async () => {
  const doc = JSON.parse(bundle([ship("ship-01", ["work"])]));
  doc.ships.push(
    { name: "../../etc", tags: ["work"] },
    { name: "Ship-02", tags: ["work"] },
    { name: "ship-03", tags: "work" },
    { name: "ship-04", tags: ["WORK", "work", "a b", "x".repeat(40), "-dash", "", 7, "<b>"] },
    null,
    "ship-05",
    { name: { toString: "ship-06" }, tags: [] },
  );
  const s = stand({ files: { [FLEET_FILE]: JSON.stringify(doc) } });
  await s.tags.read();
  assert.equal(s.tags.from, "fleet.json");
  assert.deepEqual([...s.tags.tags.keys()].sort(), ["ship-01", "ship-03", "ship-04"]);
  assert.deepEqual(s.tags.get("ship-03"), []);
  assert.deepEqual(s.tags.get("ship-04"), []); // 7 makes the list not all text: none kept
  doc.ships = [{ name: "ship-04", tags: ["WORK", "work", "a b", "x".repeat(40), "-dash", "", "<b>", "personal"] }];
  s.host.files[FLEET_FILE] = JSON.stringify(doc);
  s.tick(FILE_MS);
  await s.tags.read();
  assert.deepEqual(s.tags.get("ship-04"), ["work", "personal"]);
  assert.deepEqual(s.execs(), []);
});

test("re-read on the desk's tick: fleet.json every 30 s, ship-fleet every 5 minutes", async () => {
  const s = stand({ ls: { code: 0, stdout: JSON.stringify([lsEntry("ship-01", ["work"])]), stderr: "" } });
  assert.equal(await s.tags.read(), true);
  assert.equal(await s.tags.read(), false); // not due
  s.tick(FILE_MS - 1);
  assert.equal(await s.tags.read(), false);
  assert.equal(s.calls.filter((c) => c[0] === "read").length, 1);
  s.tick(1);
  assert.equal(await s.tags.read(), false); // fleet.json read again, ship-fleet not yet due: no change
  assert.equal(s.calls.filter((c) => c[0] === "read").length, 2);
  assert.equal(s.execs().length, 1);
  s.tick(LEDGER_MS);
  await s.tags.read();
  assert.equal(s.execs().length, 2);

  // fleet.json appears: it wins at the next read.
  s.host.files[FLEET_FILE] = bundle([ship("ship-01", ["personal"])]);
  s.tick(FILE_MS);
  assert.equal(await s.tags.read(), true);
  assert.equal(s.tags.from, "fleet.json");
  assert.deepEqual(s.tags.get("ship-01"), ["personal"]);
  // It changes: the change shows at the next read.
  s.host.files[FLEET_FILE] = bundle([ship("ship-01", ["work"])]);
  s.tick(FILE_MS);
  assert.equal(await s.tags.read(), true);
  assert.deepEqual(s.tags.get("ship-01"), ["work"]);
  // It goes (or turns bad): ship-fleet is asked at once, not after five minutes.
  s.host.files[FLEET_FILE] = "{";
  s.tick(FILE_MS);
  await s.tags.read();
  assert.equal(s.execs().length, 3);
  assert.equal(s.tags.from, "ship-fleet");
});

test("one read at a time", async () => {
  const s = stand({ files: { [FLEET_FILE]: bundle([ship("ship-01", ["work"])]) } });
  const [a, b] = await Promise.all([s.tags.read(), s.tags.read()]);
  assert.deepEqual([a, b], [true, false]);
  assert.equal(s.calls.length, 1);
});

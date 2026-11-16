// The Fleet panel's model (herdr/mods/fleet/model.js), with a stand-in for Herdr. The examples
// it reads are the repo's own, from contracts/examples/.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import {
  Fleet,
  POLL_MS,
  SCHEMA,
  ago,
  checkDestination,
  checkSchema,
  connection,
  firstLine,
  header,
  isShown,
  openPlan,
  quote,
  readAbout,
  readAttention,
  readFleetFile,
  readMachines,
  readSettings,
  readShips,
  remoteCommand,
  rows,
  selectPlan,
  shipName,
  sshArgs,
} from "../../mods/fleet/model.js";

const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const EXAMPLES = join(ROOT, "contracts", "examples");
const example = (kind, name) => JSON.parse(readFileSync(join(EXAMPLES, kind, name), "utf8"));
const examples = (kind) =>
  readdirSync(join(EXAMPLES, kind))
    .filter((f) => f.endsWith(".json"))
    .sort()
    .map((f) => [f, example(kind, f)]);

const HOME = "/home/agent";
const APP = `${HOME}/.config/herdr-app`;
const SETTINGS = `${APP}/mods/fleet/settings.json`;
const FLEET_FILE = `${HOME}/.ssh/shipshape/fleet.json`;
const DEST = "fleetledger@fleetledger.example.invalid";

// ---- the stand-in ---------------------------------------------------------------------------

/** A Herdr stand-in. `answers` maps a ship-fleet verb ("ls", "attention", "about") to
 *  {code, stdout, stderr}, a function of the ssh argv, or an Error to reject with. */
function stand({ files = {}, answers = {}, hcAnswer, clock } = {}) {
  const calls = [];
  const host = {
    home: () => HOME,
    appHome: () => APP,
    readTextFile: async (path) => {
      const f = files[path];
      if (f instanceof Error) throw f;
      return f ?? null;
    },
    exec: async (command, args, options) => {
      calls.push({ command, args, options });
      assert.equal(command, "ssh");
      const line = args.at(-1);
      const verb = /^'ship-fleet' '([a-z-]+)'/.exec(line)?.[1];
      const a = answers[verb];
      if (a === undefined) throw new Error(`no answer for ${line}`);
      if (a instanceof Error) throw a;
      const r = typeof a === "function" ? a(args) : a;
      return { code: 0, stdout: "", stderr: "", ...r };
    },
    hc: async (args) => {
      calls.push({ command: "hc", args });
      if (hcAnswer instanceof Error) throw hcAnswer;
      return hcAnswer ?? { code: 0, stdout: JSON.stringify({ machines: [] }), stderr: "" };
    },
  };
  return { host, calls, fleet: new Fleet(host, clock ?? (() => 1_000_000)) };
}

const ok = (doc) => ({ code: 0, stdout: `${JSON.stringify(doc)}\n` });
const settings = (s) => ({ [SETTINGS]: JSON.stringify(s) });
const ships = () => examples("fleetledger-ship").map(([, d]) => d);
const paths = { settings: SETTINGS };
const sshCalls = (calls) => calls.filter((c) => c.command === "ssh");

// ---- what is sent ---------------------------------------------------------------------------

test("quote makes one word for the far shell, whatever is in it", () => {
  const nasty = ["ship-01", "it's", "a b", "$(touch x)", "`id`", "; rm -rf ~", "\"x\"", "'", "", "--yes"];
  const words = execFileSync("sh", ["-c", `printf '%s\\0' ${nasty.map(quote).join(" ")}`], { encoding: "utf8" });
  assert.deepEqual(words.split("\0").slice(0, -1), nasty);
  assert.equal(quote("it's"), `'it'\\''s'`);
});

test("quote refuses a line break or control character rather than send it", () => {
  for (const bad of ["a\nb", "a\rb", "a\u0000b", "a\u001bb", "a\u007fb"]) assert.throws(() => quote(bad), /line break or control/);
});

test("remoteCommand quotes ship-fleet and every argument", () => {
  assert.equal(remoteCommand(["ls", "--json", "--offline"]), "'ship-fleet' 'ls' '--json' '--offline'");
});

test("sshArgs: batch mode, the key when given, and -- before the destination", () => {
  assert.deepEqual(sshArgs({ ssh: DEST }, ["about", "--json"]), ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "--", DEST, "'ship-fleet' 'about' '--json'"]);
  assert.deepEqual(sshArgs({ ssh: DEST, identity: `${HOME}/.ssh/id_fleet` }, ["ls"]), ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-i", `${HOME}/.ssh/id_fleet`, "-o", "IdentitiesOnly=yes", "--", DEST, "'ship-fleet' 'ls'"]);
});

test("shipName takes [a-z0-9-] names and refuses the rest", () => {
  for (const good of ["ship-01", "a", "0", "x".repeat(31)]) assert.equal(shipName(good), good);
  for (const bad of ["Ship-01", "ship_01", "-ship", "ship 01", "ship;id", "ship\n01", "", "x".repeat(32), "ship.01", null, 7]) {
    assert.throws(() => shipName(bad), /not a Ship name/, String(bad));
  }
});

test("checkDestination refuses what ssh would take as an option, and blanks", () => {
  assert.equal(checkDestination(DEST), null);
  assert.equal(checkDestination("fleetledger"), null);
  assert.equal(checkDestination("ssh://fleetledger@192.0.2.10:2222"), null);
  assert.match(checkDestination("-oProxyCommand=x"), /option/);
  assert.match(checkDestination("a b"), /space/);
  assert.match(checkDestination("a\nb"), /space or control/);
  assert.match(checkDestination(""), /empty/);
  assert.match(checkDestination(42), /empty/);
});

test("firstLine is the first line with text on it", () => {
  assert.equal(firstLine("\n  \nfirst\nsecond"), "first");
  assert.equal(firstLine("", null, "from stdout\nmore"), "from stdout");
  assert.equal(firstLine(""), "");
  assert.equal(firstLine("x".repeat(400)).length, 300);
});

// ---- reading the contract's documents --------------------------------------------------------

test("checkSchema: known, none (an older FleetLedger), newer, older, unknown", () => {
  assert.equal(checkSchema({ schema: SCHEMA.ship }, SCHEMA.ship), null);
  assert.match(checkSchema({}, SCHEMA.ship), /older than the contract.*update FleetLedger/);
  assert.match(checkSchema({ schema: "shipshape.fleetledger-ship/2" }, SCHEMA.ship), /newer.*update the panel/);
  assert.match(checkSchema({ schema: "shipshape.fleetledger-ship/0" }, SCHEMA.ship), /older.*update FleetLedger/);
  assert.match(checkSchema({ schema: "shipshape.ship-status/1" }, SCHEMA.ship), /unknown shape/);
  assert.match(checkSchema({ schema: 1 }, SCHEMA.ship), /not text/);
  assert.match(checkSchema([], SCHEMA.ship), /not a JSON object/);
});

test("readShips reads every fleetledger-ship example", () => {
  for (const [name, doc] of examples("fleetledger-ship")) {
    const got = readShips(JSON.stringify([doc]));
    assert.deepEqual(got.refused, [], name);
    assert.equal(got.ships.length, 1, name);
    assert.equal(got.ships[0].name, doc.name, name);
    assert.deepEqual(got.ships[0].tags, doc.tags, name);
    assert.equal(got.ships[0].state, doc.state, name);
  }
  const answering = readShips(JSON.stringify([example("fleetledger-ship", "answering.json")])).ships[0];
  assert.deepEqual(answering.attention, { code: "signin-missing", message: "Claude is not signed in." });
  assert.equal(answering.more, 1);
});

test("readShips leaves out entries of a shape it does not know, saying why", () => {
  const noSchema = example("fleetledger-ship/invalid", "no-schema.json");
  const newer = { ...example("fleetledger-ship", "unreachable.json"), schema: "shipshape.fleetledger-ship/2" };
  const got = readShips(JSON.stringify([...ships(), noSchema, newer, newer]));
  assert.equal(got.ships.length, ships().length);
  assert.equal(got.refused.length, 2);
  assert.match(got.refused[0], /older than the contract/);
  assert.match(got.refused[1], /newer.*\(2 entries\)/);
  const broken = readShips(JSON.stringify([{ schema: SCHEMA.ship, name: "ship-09" }]));
  assert.match(broken.refused[0], /lacks its name, tags or state/);
});

test("readShips: an empty fleet, not JSON, not a list", () => {
  assert.deepEqual(readShips("[]\n"), { ships: [], refused: [], older: false });
  assert.match(readShips("Traceback (most recent call last):").error, /ship-fleet ls: not JSON/);
  assert.match(readShips(JSON.stringify(example("fleetledger-ship", "answering.json"))).error, /not a list/);
});

test("readAttention keeps the fleet's own items, from every example", () => {
  const all = examples("fleetledger-attention").map(([, d]) => d);
  const got = readAttention(JSON.stringify(all));
  assert.deepEqual(got.refused, []);
  const want = all.filter((d) => d.ship === null).map((d) => d.code).sort();
  assert.ok(want.includes("vault-locked") && want.length >= 3);
  assert.deepEqual(got.fleet.map((i) => i.code).sort(), want);
  const locked = got.fleet.find((i) => i.code === "vault-locked");
  assert.equal(locked.severity, "high");
  assert.match(locked.fix, /ship-fleet unlock/);
  assert.match(readAttention(JSON.stringify([{ ...all[0], schema: undefined }])).refused[0], /older than the contract/);
  assert.match(readAttention("{}").error, /not a list/);
});

test("readAbout reads every fleetledger-about example", () => {
  const want = { "degraded.json": "unlocked", "desktop-checked.json": "off", "vm-locked.json": "locked" };
  for (const [name, doc] of examples("fleetledger-about")) {
    const got = readAbout(JSON.stringify(doc));
    assert.equal(got.error, undefined, name);
    assert.equal(got.version, doc.version, name);
    assert.equal(got.vault, want[name], name);
  }
  assert.match(readAbout(JSON.stringify({ version: "0.1.0" })).error, /about: no "schema" field/);
  assert.match(readAbout("usage: ship-fleet").error, /not JSON/);
});

test("readSettings: missing, good, a key path, and what it refuses", () => {
  assert.deepEqual(readSettings(null, HOME), { missing: true, warnings: [] });
  assert.deepEqual(readSettings(JSON.stringify({ ssh: DEST }), HOME), { ssh: DEST, identity: null, warnings: [] });
  assert.equal(readSettings(JSON.stringify({ ssh: DEST, identity_file: "~/.ssh/id_fleet" }), HOME).identity, `${HOME}/.ssh/id_fleet`);
  assert.equal(readSettings(JSON.stringify({ ssh: DEST, identity_file: "/keys/id_fleet" }), HOME).identity, "/keys/id_fleet");
  assert.match(readSettings(JSON.stringify({ ssh: DEST, identity_file: "id_fleet" }), HOME).error, /identity_file/);
  assert.match(readSettings(JSON.stringify({ ssh: DEST, identity_file: "-oProxyCommand=x" }), HOME).error, /identity_file/);
  assert.match(readSettings(JSON.stringify({ ssh: "-oProxyCommand=x" }), HOME).error, /"ssh" starts with '-'/);
  assert.match(readSettings("{ssh: x}", HOME).error, /settings.json: not JSON/);
  assert.match(readSettings("[]", HOME).error, /not a JSON object/);
  assert.match(readSettings(JSON.stringify({ ssh: DEST, password: "x" }), HOME).warnings[0], /"password" is not a setting/);
  assert.equal(readSettings(JSON.stringify({}), HOME).missing, true);
});

test("readFleetFile reads every machine-bundle example", () => {
  assert.deepEqual(readFleetFile(null), { absent: true });
  for (const [name, doc] of examples("machine-bundle")) {
    const got = readFleetFile(JSON.stringify(doc));
    assert.equal(got.error, undefined, name);
    assert.deepEqual(got, { ships: doc.ships.map((s) => ({ name: s.name, tags: s.tags })) }, name);
  }
});

test("readFleetFile: the shape is checked, and names outside [a-z0-9-] are left out", () => {
  const doc = example("machine-bundle", "renewed.json");
  assert.match(readFleetFile(JSON.stringify({ ...doc, schema: "shipshape.machine-bundle/2" })).error, /fleet.json: shape .* newer/);
  assert.match(readFleetFile(JSON.stringify({ ...doc, schema: undefined })).error, /fleet.json: no "schema" field/);
  const odd = { ...doc, ships: [...doc.ships, { name: "Bad Name", tags: [] }] };
  const got = readFleetFile(JSON.stringify(odd));
  assert.deepEqual(got.ships.map((s) => s.name), ["ship-01"]);
});

test("readMachines: by name, without temporary ones", () => {
  const out = JSON.stringify({ machines: [{ name: "ship-01", state: "connected" }, { name: "ship-02", state: "needs_login", why: "certificate expired" }, { name: "tmp", state: "connected", temporary: true }] });
  assert.deepEqual(readMachines(out).byName, { "ship-01": { state: "connected", why: null }, "ship-02": { state: "needs_login", why: "certificate expired" } });
  assert.match(readMachines("{}").error, /no machines list/);
});

// ---- one read, end to end --------------------------------------------------------------------

const answersFor = (shipList, { attention = [], about = example("fleetledger-about", "desktop-checked.json") } = {}) => ({
  ls: ok(shipList),
  attention: ok(attention),
  about: ok(about),
});

test("missing settings: nothing is sent, and the panel says where to put them", async () => {
  const { fleet, calls } = stand();
  await fleet.refresh();
  assert.deepEqual(sshCalls(calls), []);
  const h = header(fleet.state, 1_000_000, paths);
  assert.match(h.notices[0].text, /Where is FleetLedger\?.*"ssh".*mods\/fleet\/settings.json/);
  assert.equal(h.notices[0].tone, "warn");
  assert.equal(h.empty, "No Ships to show.");
  assert.deepEqual(rows(fleet.state, { spaces: [], machines: [] }), []);
});

test("a read: the three offline reads, exactly, and the rows", async () => {
  const { fleet, calls } = stand({
    files: settings({ ssh: DEST }),
    answers: answersFor(ships(), { attention: [example("fleetledger-attention", "machine-cert-expiring.json")] }),
    hcAnswer: { code: 0, stdout: JSON.stringify({ machines: [{ name: "ship-02", state: "unreachable", why: "no route" }] }) },
  });
  await fleet.refresh();
  const lines = sshCalls(calls).map((c) => c.args.at(-1)).sort();
  assert.deepEqual(lines, ["'ship-fleet' 'about' '--json'", "'ship-fleet' 'attention' '--offline' '--json'", "'ship-fleet' 'ls' '--json' '--offline'"]);
  for (const c of sshCalls(calls)) {
    assert.deepEqual(c.args.slice(0, 6), ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "--", DEST]);
    assert.equal(c.options.timeout, 20000);
    assert.ok(!c.args.some((a) => a.includes("--yes")));
  }
  const live = { spaces: [{ id: "s2", machine: "ship-01", tabs: [] }], machines: [{ name: "ship-01", layout: "ready" }] };
  const r = rows(fleet.state, live);
  assert.deepEqual(r.map((x) => x.name), ships().map((s) => s.name));
  const one = r.find((x) => x.name === "ship-01");
  assert.equal(one.state, "needs-sign-in");
  assert.equal(one.tone, "warn");
  assert.equal(one.note, "Claude is not signed in. (+1 more)");
  assert.deepEqual(one.conn, { text: "connected", tone: "good" });
  assert.equal(one.space, "s2");
  const two = r.find((x) => x.name === "ship-02");
  assert.equal(two.tone, "bad");
  assert.equal(two.note, "Cannot reach ship-02");
  assert.equal(two.conn.text, "unreachable");
  assert.equal(r.find((x) => x.name === "ship-03").conn.text, "not in Herdr");
  const h = header(fleet.state, 1_000_000 + 4 * 60_000, paths);
  assert.deepEqual(h.facts, [`FleetLedger: ${DEST}`, "version 0.1.0", "vault off", "read 4 min ago"]);
  assert.equal(h.locked, false);
  assert.ok(h.notices.some((n) => /certificate/.test(n.text) && n.tone === "warn"));
});

test("a locked vault shows in the header, with the fix", async () => {
  const { fleet } = stand({
    files: settings({ ssh: DEST }),
    answers: answersFor(ships(), { attention: [example("fleetledger-attention", "vault-locked.json")], about: example("fleetledger-about", "vm-locked.json") }),
  });
  await fleet.refresh();
  const h = header(fleet.state, 1_000_000, paths);
  assert.equal(h.locked, true);
  assert.ok(h.facts.includes("vault locked"));
  const n = h.notices.find((x) => /^Vault locked/.test(x.text));
  assert.equal(n.tone, "bad");
  assert.match(n.text, /ship-fleet unlock/);
});

test("a locked vault on a FleetLedger without about still shows, from attention", async () => {
  const { fleet } = stand({
    files: settings({ ssh: DEST }),
    answers: { ls: ok([]), attention: ok([example("fleetledger-attention", "vault-locked.json")]), about: { code: 2, stderr: "usage: ship-fleet ...\nship-fleet: error: argument command: invalid choice: 'about'" } },
  });
  await fleet.refresh();
  const h = header(fleet.state, 1_000_000, paths);
  assert.equal(h.locked, true);
  assert.ok(h.facts.includes("version unknown"));
  assert.ok(h.facts.includes("vault locked"));
  assert.ok(h.notices.some((x) => x.text === "ship-fleet about: this FleetLedger is older than the contract this panel reads; update FleetLedger (ship-fleet: error: argument command: invalid choice: 'about')"));
});

test("an older FleetLedger without --offline is named plainly", async () => {
  const { fleet } = stand({ files: settings({ ssh: DEST }), answers: { ls: { code: 2, stderr: "usage: ship-fleet ls [-h] [--json]\nship-fleet ls: error: unrecognized arguments: --offline\n" }, attention: ok([]), about: ok(example("fleetledger-about", "desktop-checked.json")) } });
  await fleet.refresh();
  assert.equal(fleet.state.error, "ship-fleet ls: this FleetLedger is older than the contract this panel reads; update FleetLedger (ship-fleet ls: error: unrecognized arguments: --offline)");
});

test("a failed read drops the last read's left-out notices", async () => {
  let up = true;
  const newer = { ...example("fleetledger-ship", "unreachable.json"), schema: "shipshape.fleetledger-ship/2" };
  const { fleet } = stand({ files: settings({ ssh: DEST }), answers: { ls: () => (up ? ok([newer]) : { code: 1, stderr: "boom\n" }), attention: ok([]), about: ok(example("fleetledger-about", "desktop-checked.json")) } });
  await fleet.refresh();
  assert.equal(fleet.state.refused.length, 1);
  up = false;
  await fleet.refresh();
  assert.deepEqual(fleet.state.refused, []);
  assert.ok(!header(fleet.state, 1_000_000, paths).notices.some((n) => /Left out/.test(n.text)));
});

test("an empty fleet says so", async () => {
  const { fleet } = stand({ files: settings({ ssh: DEST }), answers: answersFor([]) });
  await fleet.refresh();
  assert.equal(header(fleet.state, 1_000_000, paths).empty, "FleetLedger has no Ships yet.");
});

test("ssh cannot connect: its first stderr line, and nothing more is asked", async () => {
  const { fleet, calls } = stand({
    files: settings({ ssh: DEST }),
    answers: { ls: { code: 255, stderr: "ssh: connect to host fleetledger.example.invalid port 22: Connection refused\r\nmore\n" } },
  });
  await fleet.refresh();
  assert.equal(sshCalls(calls).length, 1);
  assert.equal(fleet.state.error, `Couldn't reach FleetLedger at ${DEST}: ssh: connect to host fleetledger.example.invalid port 22: Connection refused`);
  const h = header(fleet.state, 1_000_000, paths);
  assert.ok(h.notices.some((n) => n.tone === "bad" && n.text === fleet.state.error));
  assert.equal(h.empty, "No list read yet.");
});

test("ssh will not start: the reason", async () => {
  const { fleet } = stand({ files: settings({ ssh: DEST }), answers: { ls: new Error("No such file or directory (os error 2)\nat exec") } });
  await fleet.refresh();
  assert.equal(fleet.state.error, "Couldn't run ssh: No such file or directory (os error 2)");
});

test("ship-fleet refuses: the first line of what it said", async () => {
  const { fleet } = stand({
    files: settings({ ssh: DEST }),
    answers: { ls: { code: 1, stderr: "ship-fleet: this copy is retired; the fleet moved\nsee ship-fleet migrate\n" }, attention: { code: 1, stderr: "retired\n" }, about: ok({ ...example("fleetledger-about", "degraded.json"), copy: "retired" }) },
  });
  await fleet.refresh();
  assert.equal(fleet.state.error, "ship-fleet ls: ship-fleet: this copy is retired; the fleet moved");
  assert.ok(header(fleet.state, 1_000_000, paths).facts.includes("copy: retired"));
});

test("a failed read keeps the last list, and says when it was read", async () => {
  let now = 1_000_000;
  let up = true;
  const { fleet } = stand({
    files: settings({ ssh: DEST }),
    answers: { ls: () => (up ? ok(ships()) : { code: 255, stderr: "Connection timed out" }), attention: ok([]), about: ok(example("fleetledger-about", "degraded.json")) },
    clock: () => now,
  });
  await fleet.refresh();
  up = false;
  now += POLL_MS;
  await fleet.refresh();
  assert.equal(fleet.state.ships.length, ships().length);
  const h = header(fleet.state, now, paths);
  assert.ok(h.facts.includes("read 5 min ago"));
  assert.match(fleet.state.error, /Connection timed out/);
});

test("settings that are wrong: said plainly, nothing sent", async () => {
  const { fleet, calls } = stand({ files: { [SETTINGS]: '{"ssh": "-oProxyCommand=x"}' } });
  await fleet.refresh();
  assert.deepEqual(sshCalls(calls), []);
  assert.ok(header(fleet.state, 1_000_000, paths).notices.some((n) => n.tone === "bad" && /starts with '-'/.test(n.text)));
});

test("an unreadable settings file: the reason, not silence", async () => {
  const { fleet } = stand({ files: { [SETTINGS]: new Error("Permission denied (os error 13)") } });
  await fleet.refresh();
  assert.match(header(fleet.state, 1_000_000, paths).notices[0].text, /Couldn't read .*settings.json: Permission denied/);
});

test("the key file goes to ssh with IdentitiesOnly", async () => {
  const { fleet, calls } = stand({ files: settings({ ssh: DEST, identity_file: "~/.ssh/id_fleet" }), answers: answersFor([]) });
  await fleet.refresh();
  for (const c of sshCalls(calls)) assert.deepEqual(c.args.slice(4, 8), ["-i", `${HOME}/.ssh/id_fleet`, "-o", "IdentitiesOnly=yes"]);
});

const bundle = (over = {}) => ({ ...example("machine-bundle", "renewed.json"), ...over });

test("fleet.json never says where to ask: a refresher's key may only fetch its bundle", async () => {
  const { fleet, calls } = stand({ files: { [FLEET_FILE]: JSON.stringify(bundle()) } });
  await fleet.refresh();
  assert.deepEqual(sshCalls(calls), []);
  assert.equal(fleet.state.where, null);
  assert.equal(fleet.state.shipsFrom, "fleet.json");
});

test("a key forced to machine-bundle: said, and not used again", async () => {
  let now = 1_000_000;
  const forced = { ls: ok(bundle()), attention: ok(bundle()), about: ok(bundle()) };
  const { fleet, calls } = stand({ files: settings({ ssh: DEST }), answers: forced, clock: () => now });
  await fleet.refresh();
  assert.equal(sshCalls(calls).length, 1);
  assert.match(fleet.state.error, /answered with this machine's bundle.*will not ask with it again/);
  now += POLL_MS;
  await fleet.refresh();
  assert.equal(sshCalls(calls).length, 1);
  assert.match(header(fleet.state, now, paths).notices.find((n) => n.tone === "bad").text, /machine's bundle/);
});

test("a key forced to machine-bundle: another key in settings is asked", async () => {
  const files = settings({ ssh: DEST });
  let lsAnswer = ok(bundle());
  const { fleet, calls } = stand({ files, answers: { ls: () => lsAnswer, attention: ok([]), about: ok(example("fleetledger-about", "desktop-checked.json")) } });
  await fleet.refresh();
  files[SETTINGS] = JSON.stringify({ ssh: DEST, identity_file: "~/.ssh/id_fleet" });
  lsAnswer = ok(ships());
  await fleet.refresh();
  assert.equal(sshCalls(calls).length, 4);
  assert.equal(fleet.state.error, null);
  assert.equal(fleet.state.ships.length, ships().length);
});

test("fleet.json: its Ships when FleetLedger cannot be read", async () => {
  const doc = bundle({ fleetledger: null });
  const { fleet, calls } = stand({ files: { [FLEET_FILE]: JSON.stringify(doc) } });
  await fleet.refresh();
  assert.deepEqual(sshCalls(calls), []);
  const r = rows(fleet.state, { spaces: [], machines: [] });
  assert.deepEqual(r.map((x) => [x.name, x.tags, x.state, x.tone]), [["ship-01", ["work"], "—", "dim"]]);
  const h = header(fleet.state, 1_000_000, paths);
  assert.ok(h.facts.includes("Ships from fleet.json"));
  assert.ok(h.notices.some((n) => n.tone === "dim" && /Ships from fleet.json only/.test(n.text)));
});

test("fleet.json of a shape the panel does not know is said, not used", async () => {
  const { fleet } = stand({ files: { ...settings({ ssh: DEST }), [FLEET_FILE]: JSON.stringify({ ssh: "elsewhere" }) }, answers: answersFor([]) });
  await fleet.refresh();
  assert.ok(header(fleet.state, 1_000_000, paths).notices.some((n) => /fleet.json: no "schema" field/.test(n.text)));
});

test("hc fails: Herdr's connection state says so", async () => {
  const { fleet } = stand({ files: settings({ ssh: DEST }), answers: answersFor(ships()), hcAnswer: new Error("hc: not found") });
  await fleet.refresh();
  assert.equal(fleet.state.machinesError, "Couldn't run hc: hc: not found");
  const { fleet: f2 } = stand({ files: settings({ ssh: DEST }), answers: answersFor(ships()), hcAnswer: { code: 1, stderr: "no core running\n" } });
  await f2.refresh();
  assert.equal(f2.state.machinesError, "hc machine ls: no core running");
});

test("due: none read yet, then every POLL_MS; two refreshes at once make one read", async () => {
  let now = 1_000_000;
  const { fleet, calls } = stand({ files: settings({ ssh: DEST }), answers: answersFor([]), clock: () => now });
  assert.equal(fleet.due(), true);
  await Promise.all([fleet.refresh(), fleet.refresh()]);
  assert.equal(sshCalls(calls).length, 3);
  assert.equal(fleet.due(), false);
  now += POLL_MS - 1;
  assert.equal(fleet.due(), false);
  now += 1;
  assert.equal(fleet.due(), true);
});

// ---- the view's decisions --------------------------------------------------------------------

const space = (id, active, tabs) => ({ id, active, machine: null, tabs: tabs.map(([tid, a]) => ({ id: tid, active: a })) });

test("isShown: only when its tab is the shown one of the shown space", () => {
  const panes = [{ pane: "p1", terminal: null, mod: "fleet", tab: "t2" }, { pane: "p2", terminal: "t9", tab: "t1" }];
  assert.equal(isShown(panes, [space("s1", true, [["t1", false], ["t2", true]])]), true);
  assert.equal(isShown(panes, [space("s1", true, [["t1", true], ["t2", false]])]), false);
  assert.equal(isShown(panes, [space("s1", false, [["t2", true]]), space("s2", true, [["t3", true]])]), false);
  // In no tab (a layout block or a window): it runs only where Herdr shows it.
  assert.equal(isShown([], []), true);
});

test("openPlan: the Fleet tab when there is one, else a new tab", () => {
  const panes = [{ pane: "p1", terminal: null, mod: "fleet", tab: "t2" }];
  assert.deepEqual(openPlan(panes, [space("s1", false, [["t2", false]])]), [["space.select", { id: "s1" }], ["tab.select", { id: "t2" }]]);
  assert.deepEqual(openPlan([], []), [["tab.new", { name: "Fleet", mod: "fleet" }]]);
});

test("selectPlan: the Ship's space, or why not", () => {
  assert.deepEqual(selectPlan({ name: "ship-01", named: true, space: "s2" }), { run: ["space.select", { id: "s2" }] });
  assert.match(selectPlan({ name: "ship-01", named: true, space: null }).note, /No Herdr space on ship-01/);
  assert.match(selectPlan({ name: "Ship 01", named: false, space: null }).note, /not a name/);
});

test("rows never point a name outside [a-z0-9-] at a space", () => {
  const state = { ships: [{ name: "Ship_01", tags: [], state: "ready", problem: null, message: null, attention: null, more: 0 }], machines: {} };
  const [r] = rows(state, { spaces: [{ id: "s1", machine: "Ship_01" }], machines: [] });
  assert.equal(r.named, false);
  assert.equal(r.space, null);
});

test("connection: Herdr's live layout first, then hc's word", () => {
  assert.deepEqual(connection("ship-01", {}, [{ name: "ship-01", layout: "ready" }]), { text: "connected", tone: "good" });
  assert.deepEqual(connection("ship-01", { "ship-01": { state: "needs_login", why: null } }, []), { text: "needs login", tone: "warn", why: null });
  assert.equal(connection("ship-01", {}, [{ name: "ship-01", layout: "loading" }]).text, "connecting");
  assert.equal(connection("ship-01", {}, [{ name: "ship-01", layout: "unavailable" }]).tone, "bad");
});

test("ago", () => {
  assert.equal(ago(10_000), "just now");
  assert.equal(ago(4 * 60_000), "4 min ago");
  assert.equal(ago(3 * 3600_000), "3 h ago");
  assert.equal(ago(3 * 86400_000), "3 days ago");
});

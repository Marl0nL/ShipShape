// The Fleet panel's Machines view (herdr/mods/fleet/model.js): this computer's refresher and the
// machines trusted to attach. With a stand-in for Herdr; the documents it reads are the repo's
// own examples, from contracts/examples/.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import {
  ED25519_BLOB,
  Fleet,
  NO_REFRESHER,
  REFRESHER_MS,
  checkDevice,
  checkPublicKey,
  checkScopes,
  header,
  localFact,
  machineMenu,
  machineRows,
  machinesView,
  pairJob,
  readCa,
  readMachineConf,
  readRefreshState,
  trustJob,
} from "../../mods/fleet/model.js";

const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const EXAMPLES = join(ROOT, "contracts", "examples");
const text = (kind, name) => readFileSync(join(EXAMPLES, kind, name), "utf8");
const example = (kind, name) => JSON.parse(text(kind, name));
const names = (kind, sub = "") =>
  readdirSync(join(EXAMPLES, kind, sub))
    .filter((f) => f.endsWith(".json"))
    .sort();

const HOME = "/home/agent";
const APP = `${HOME}/.config/herdr-app`;
const SETTINGS = `${APP}/mods/fleet/settings.json`;
const D = `${HOME}/.ssh/shipshape`;
const REFRESHER = `${D}/refresh`;
const STATE = `${D}/state.json`;
const CONF = `${D}/machine.conf`;
const DEST = "fleetledger@fleetledger.example.invalid";
const SKIPPING = ["--yes", "-y", "--force", "--revoke-my-key", "--stdin"];
// A made-up ed25519 public key: the 19 bytes of its type, then 32 bytes.
const BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
const KEY = `ssh-ed25519 ${BLOB}`;
const NOW = Date.parse("2026-01-05T10:00:00Z");

const ok = (doc) => ({ code: 0, stdout: `${JSON.stringify(doc)}\n`, stderr: "" });

/** A Herdr stand-in. `files` by path (null: absent); `answers` by ship-fleet verb; `refresher`:
 *  null (none) or what it answers, and `after` files it leaves when it runs. */
function stand({ files = {}, answers = {}, refresher = null, after = {}, about = example("fleetledger-about", "desktop-checked.json") } = {}) {
  const calls = [];
  const all = {
    ls: ok(names("fleetledger-ship").map((f) => example("fleetledger-ship", f))),
    attention: ok([]),
    about: ok(about),
    ca: ok(example("fleetledger-ca", "fleet.json")),
    ...answers,
  };
  const disk = { [SETTINGS]: JSON.stringify({ ssh: DEST }), ...files };
  const host = {
    home: () => HOME,
    appHome: () => APP,
    readTextFile: async (path) => disk[path] ?? null,
    exec: async (command, args, options) => {
      calls.push({ command, args, options });
      if (command === "ssh") {
        const verb = /^'ship-fleet' '([a-z-]+)'/.exec(args.at(-1))?.[1];
        const a = all[verb];
        if (a === undefined) throw new Error(`no answer for ${args.at(-1)}`);
        return typeof a === "function" ? a(args) : a;
      }
      if (command === "test") return { code: refresher ? 0 : 1, stdout: "", stderr: "" };
      if (command === REFRESHER) {
        Object.assign(disk, after);
        return refresher;
      }
      throw new Error(`unexpected ${command}`);
    },
    hc: async () => ({ code: 0, stdout: JSON.stringify({ machines: [] }), stderr: "" }),
    run: () => null,
    notify: () => null,
  };
  return { host, calls, disk, fleet: new Fleet(host, () => NOW) };
}

const verbs = (calls) => calls.filter((c) => c.command === "ssh").map((c) => /'ship-fleet' '([a-z-]+)'/.exec(c.args.at(-1))[1]);

/** What `sh` makes of a line: its words, as a program would get them. */
function shWords(line) {
  const out = execFileSync("sh", ["-c", `printf '%s\\0' ${line}`], { encoding: "utf8" });
  return out.split("\0").slice(0, -1);
}

async function opened(opts) {
  const s = stand(opts);
  await s.fleet.openMachines();
  return s;
}

const FORM = { name: "laptop", key: KEY, scope: ["tag:work"], scopeTyped: "", logins: "agent" };

// ---- reading -------------------------------------------------------------------------------

test("readCa reads the contract's examples, and leaves out each invalid entry saying why", () => {
  const r = readCa(text("fleetledger-ca", "fleet.json"));
  assert.deepEqual(r.refused, []);
  assert.deepEqual(r.machines.map((m) => [m.name, m.state, m.revoked !== null, m.thisComputer]), [
    ["desk", "lapsed", false, false],
    ["fleetledger", "ok", false, true],
    ["old-laptop", "revoked", true, false],
  ]);
  assert.deepEqual(r.machines[0].logins, ["agent"]);
  assert.equal(r.machines[0].idleLimit, true);
  assert.equal(r.machines[0].bundleKey, null); // absent off FleetLedger's own VM
  assert.deepEqual(readCa(text("fleetledger-ca", "no-ships-yet.json")), { machines: [], refused: [] });
  for (const f of names("fleetledger-ca", "invalid")) {
    const got = readCa(text("fleetledger-ca", join("invalid", f)));
    assert.ok(got.error || (got.machines.length === 0 && got.refused.length === 1), f);
  }
  assert.match(readCa("{").error, /not JSON/);
  assert.match(readCa(JSON.stringify({ schema: "shipshape.fleetledger-ca/2", machines: [] })).error, /newer than this panel/);
});

test("readCa: a hostile name, scope, login or key state never reaches a row", () => {
  const good = example("fleetledger-ca", "fleet.json").machines[0];
  const bad = [
    { machine: "laptop; rm -rf ~" },
    { machine: "-laptop" },
    { machine: "Laptop" },
    { scope: ["tag:work'"] },
    { scope: [] },
    { logins: ["agent", "agent"] },
    { logins: ["root"] },
    { state: "fine" },
    { fingerprint: "SHA256:x" },
    { bundle_key: "everything" },
    { idle_refused: "yesterday" },
  ];
  const doc = { ...example("fleetledger-ca", "fleet.json"), machines: [good, ...bad.map((b) => ({ ...good, ...b }))] };
  const r = readCa(JSON.stringify(doc));
  assert.deepEqual(r.machines.map((m) => m.name), ["desk"]);
  assert.equal(r.refused.length, 7); // one line per reason: the names share one, the scopes and the logins theirs
});

test("readRefreshState: every example, the whole shape, nothing else", () => {
  for (const f of names("refresh-state")) assert.equal(readRefreshState(text("refresh-state", f)).error, undefined, f);
  for (const f of names("refresh-state", "invalid")) assert.ok(readRefreshState(text("refresh-state", join("invalid", f))).error, f);
  const ok = example("refresh-state", "ok.json");
  assert.deepEqual(readRefreshState(JSON.stringify(ok)), {
    machine: "laptop",
    lastAttempt: "2026-01-05T09:00:00Z",
    lastSuccess: "2026-01-05T09:00:00Z",
    certificateEnd: "2026-01-12T09:00:00Z",
    ships: 3,
    lastError: null,
    warning: null,
  });
  assert.deepEqual(readRefreshState(null), { absent: true });
  assert.match(readRefreshState(JSON.stringify({ ...ok, extra: 1 })).error, /"extra" is not in its contract/);
  const { warning: _, ...noWarning } = ok;
  assert.match(readRefreshState(JSON.stringify(noWarning)).error, /"warning" is missing/);
  assert.match(readRefreshState(JSON.stringify({ ...ok, last_error: "\u001b[31mred" })).error, /"last_error"/);
  assert.match(readRefreshState(JSON.stringify({ ...ok, last_error: "x".repeat(2001) })).error, /"last_error"/);
  assert.match(readRefreshState(JSON.stringify({ ...ok, certificate_valid_before: "soon" })).error, /certificate_valid_before/);
  assert.match(readRefreshState(JSON.stringify({ ...ok, schema: "shipshape.refresh-state/2" })).error, /newer than this panel/);
});

test("readMachineConf: only its name, and only a machine name", () => {
  assert.deepEqual(readMachineConf("# settings\nname=laptop\nkey=/home/agent/.ssh/shipshape/id_ed25519\nhosts=fleetledger.example.invalid\n"), { name: "laptop" });
  assert.deepEqual(readMachineConf("name=laptop\r\n"), { name: "laptop" });
  assert.deepEqual(readMachineConf(null), { absent: true });
  assert.match(readMachineConf("key=/x\n").error, /no name= line/);
  for (const bad of ["name=Laptop", "name=lap top", "name=$(id)", "name="]) assert.match(readMachineConf(bad).error, /not a machine name/, bad);
});

// ---- typed values ----------------------------------------------------------------------------

test("checkPublicKey: one ed25519 line; the comment is dropped, never sent", () => {
  assert.equal(checkPublicKey(KEY), KEY);
  assert.equal(checkPublicKey(`${KEY} agent-laptop\n`), KEY);
  assert.equal(checkPublicKey(`  ${KEY} it's "mine"; $(rm -rf ~) \`x\`\r\n\n`), KEY);
  assert.equal(checkPublicKey(`ssh-ed25519\t${BLOB}\tcomment`), KEY);
  assert.ok(ED25519_BLOB.test(BLOB));
});

test("checkPublicKey: refuses a second line, options, other types, a private key, a cut key", () => {
  const refused = [
    ["", /Paste the new machine's public key/],
    [`${KEY}\n${KEY}`, /one line only/],
    [`${KEY} laptop\nssh-ed25519 ${BLOB} other`, /one line only/],
    [`${KEY}\rinjected`, /one line only/],
    [`${KEY} a\u0000b`, /control character/],
    [`restrict,command="sh" ${KEY}`, /not a key ship-fleet trusts/],
    [`command="/bin/sh -c 'id'" ${KEY}`, /not a key ship-fleet trusts/],
    [`from="*" ${KEY}`, /not a key ship-fleet trusts/],
    [`ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ laptop`, /not a key ship-fleet trusts/],
    [`ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAI laptop`, /not a key ship-fleet trusts/],
    [`ssh-ed25519-cert-v01@openssh.com AAAAIHNzaC1lZDI1NTE5LWNlcnQtdjAxQG9wZW5zc2guY29t`, /not a key ship-fleet trusts/],
    [`sk-ssh-ed25519@openssh.com ${BLOB}`, /not a key ship-fleet trusts/], // an OpenSSH key type, not an address: hygiene: allow
    ["-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----", /private key/],
    ["ssh-ed25519", /cut or changed/],
    [`ssh-ed25519 ${BLOB.slice(0, -1)}`, /cut or changed/],
    [`ssh-ed25519 ${BLOB}A`, /cut or changed/],
    [`ssh-ed25519 ${BLOB.slice(0, 25)}Q${BLOB.slice(26)}`, /cut or changed/], // past the 32 bytes' first bits
    [`ssh-ed25519 'AAAA'`, /cut or changed/],
    ["laptop.pub", /not a key ship-fleet trusts/], // a path: ship-fleet would read a file over there
    ["~/.ssh/id_ed25519", /not a key ship-fleet trusts/],
  ];
  for (const [value, why] of refused) assert.throws(() => checkPublicKey(value), why, JSON.stringify(value));
});

test("checkScopes: all, tag:TAG or ship:NAME; all alone", () => {
  assert.deepEqual(checkScopes(["all"]), ["all"]);
  assert.deepEqual(checkScopes(["tag:work"], " ship:ship-03, tag:work "), ["tag:work", "ship:ship-03"]);
  for (const bad of ["work", "tag:Work", "tag:", "ship:ship 01", "tag:work'", "tag:$(id)", "all;id", "--scope"]) assert.throws(() => checkScopes([], bad), /not a scope/, bad);
  assert.throws(() => checkScopes([], ""), /Choose what it may reach/);
  assert.throws(() => checkScopes(["all", "tag:work"]), /choose it alone/);
});

test("checkDevice: FleetLedger's device names, spaces folded", () => {
  assert.equal(checkDevice("  my   phone "), "my phone");
  assert.equal(checkDevice("Pixel (work)"), "Pixel (work)");
  assert.equal(checkDevice("it's mine"), "it's mine");
  for (const bad of ["", "-phone", "phone;id", "phone$(id)", "a".repeat(41), "phone\nx", "phone\tx", 'say "hi"']) assert.throws(() => checkDevice(bad), /device name/, bad);
});

// ---- the jobs --------------------------------------------------------------------------------

test("Trust a machine: one ssh with a terminal, every value quoted, and what is shown is what runs", async () => {
  const { fleet } = await opened();
  const plan = trustJob(fleet.state, { ...FORM, key: `${KEY} it's "my" laptop`, scopeTyped: "ship:ship-03" });
  assert.deepEqual(plan.errors, []);
  const j = plan.job;
  assert.equal(j.title, "trust laptop");
  assert.equal(j.kind, "trust");
  assert.deepEqual(j.argv.slice(0, 6), ["ssh", "-t", "-o", "ConnectTimeout=8", "--", DEST]);
  assert.deepEqual(shWords(j.remote), ["ship-fleet", "trust", "laptop", "--key", KEY, "--scope", "tag:work", "--scope", "ship:ship-03", "--logins", "agent"]);
  assert.deepEqual(shWords(j.shown), j.argv);
  assert.ok(!j.remote.includes("my")); // the comment stayed here
});

test("Trust a machine: hostile names, keys and scopes are refused, and nothing is built", async () => {
  const { fleet } = await opened();
  const cases = [
    [{ name: "" }, /Give it a name/],
    [{ name: "laptop; rm -rf ~" }, /not a machine name/],
    [{ name: "$(id)" }, /not a machine name/],
    [{ name: "-laptop" }, /not a machine name/],
    [{ name: "it's" }, /not a machine name/],
    [{ name: "Laptop" }, /not a machine name/],
    [{ name: "a".repeat(32) }, /not a machine name/],
    [{ name: "desk" }, /already a machine here/],
    [{ name: "old-laptop" }, /already a machine here/], // untrusted names are never reused
    [{ key: `${KEY}\nssh-ed25519 ${BLOB}` }, /one line only/],
    [{ key: `command="id" ${KEY}` }, /not a key ship-fleet trusts/],
    [{ key: "'; id; '" }, /not a key ship-fleet trusts/],
    [{ scope: [], scopeTyped: "" }, /Choose what it may reach/],
    [{ scopeTyped: "tag:work' --logins owner" }, /not a scope/],
    [{ logins: "root" }, /Choose its logins/],
    [{ logins: "" }, /Choose its logins/],
  ];
  for (const [change, why] of cases) {
    const plan = trustJob(fleet.state, { ...FORM, ...change });
    assert.equal(plan.job, undefined, JSON.stringify(change));
    assert.ok(plan.errors.some((e) => why.test(e)), `${JSON.stringify(change)}: ${plan.errors}`);
  }
});

test("Trust a machine needs the list and an unlocked, active FleetLedger", async () => {
  const locked = await opened({ about: example("fleetledger-about", "vm-locked.json") });
  assert.match(trustJob(locked.fleet.state, FORM).errors[0], /vault is locked/);
  const s = stand();
  await s.fleet.refresh(); // the list not read yet
  assert.match(trustJob(s.fleet.state, FORM).errors[0], /have not been read/);
});

test("a machine's menu: Allow again only while refused for idleness; Untrust always, never skipping its question", async () => {
  const doc = example("fleetledger-ca", "fleet.json");
  doc.machines[0].idle_refused = "2026-01-04T00:00:00Z";
  const { fleet } = await opened({ answers: { ca: ok(doc) } });
  const rows = machineRows(fleet.state, NOW);
  const desk = machineMenu(fleet.state, rows.find((r) => r.name === "desk"), NOW);
  assert.deepEqual(desk.items.map((i) => i.id), ["allow-again", "untrust"]);
  assert.deepEqual(shWords(desk.items[0].job.remote), ["ship-fleet", "trust", "desk", "--allow-again"]);
  assert.deepEqual(shWords(desk.items[1].job.remote), ["ship-fleet", "untrust", "desk"]);
  assert.match(desk.items[1].note, /type the machine's name in the terminal/);
  const own = machineMenu(fleet.state, rows.find((r) => r.name === "fleetledger"), NOW);
  assert.deepEqual(own.items.map((i) => i.id), ["untrust"]);
  const gone = machineMenu(fleet.state, rows.find((r) => r.name === "old-laptop"), NOW);
  assert.deepEqual(gone.items, []);
  assert.match(gone.why, /stays so/);
  assert.match(machineMenu(fleet.state, { name: "x;id" }).why, /Not a name/);
  for (const item of [...desk.items, ...own.items]) {
    const words = [...item.job.argv, ...shWords(item.job.remote)];
    for (const flag of SKIPPING) assert.ok(!words.includes(flag), `${flag} in ${item.job.shown}`);
    assert.deepEqual(shWords(item.job.shown), item.job.argv);
  }
});

test("Pair a phone's browser: ui link --qr in a terminal, only while the remote web UI is on", async () => {
  const about = { ...example("fleetledger-about", "desktop-checked.json"), ui: { remote: "https://fleetledger.example.invalid" } };
  const { fleet } = await opened({ about });
  const plan = pairJob(fleet.state, "my phone");
  assert.deepEqual(shWords(plan.job.remote), ["ship-fleet", "ui", "link", "--name", "my phone", "--qr"]);
  assert.deepEqual(shWords(plan.job.shown), plan.job.argv);
  assert.match(pairJob(fleet.state, "phone'; id; '").errors[0], /device name/);
  const off = await opened();
  assert.match(pairJob(off.fleet.state, "phone").errors[0], /not on for other devices/);
});

// ---- reading, and only when asked ------------------------------------------------------------

test("the list is read when the view opens, with a read's ssh, and not by the panel's own reads", async () => {
  const s = stand();
  await s.fleet.refresh();
  await s.fleet.refresh();
  assert.ok(!verbs(s.calls).includes("ca")); // no polling of the list
  s.calls.length = 0;
  await s.fleet.openMachines();
  assert.deepEqual(verbs(s.calls), ["ca"]);
  const ca = s.calls.find((c) => c.command === "ssh");
  assert.ok(ca.args.includes("BatchMode=yes"));
  assert.equal(ca.args.at(-1), "'ship-fleet' 'ca' 'show' '--json'");
  assert.deepEqual(s.fleet.state.ca.machines.map((m) => m.name), ["desk", "fleetledger", "old-laptop"]);
});

test("a failed read keeps the last list and says why, with its first line", async () => {
  const s = await opened();
  s.host.exec = async (command, args) => (command === "ssh" ? { code: 1, stdout: "", stderr: "\nThere is no CA yet: ship-fleet ca init.\nmore\n" } : { code: 1, stdout: "", stderr: "" });
  await s.fleet.loadCa();
  assert.equal(s.fleet.state.ca.machines.length, 3);
  assert.equal(s.fleet.state.ca.error, "ship-fleet ca: There is no CA yet: ship-fleet ca init.");
  const v = machinesView(s.fleet.state, NOW);
  assert.ok(v.notices.some((n) => n.tone === "bad" && /no CA yet/.test(n.text)));
});

test("a pull-only computer: no ship-fleet call, only its own refresh, and why in one line", async () => {
  const files = { [SETTINGS]: null, [STATE]: text("refresh-state", "ok.json"), [CONF]: "name=laptop\n" };
  const s = await opened({ files, refresher: { code: 0, stdout: "", stderr: "" } });
  assert.deepEqual(verbs(s.calls), []);
  const v = machinesView(s.fleet.state, NOW);
  assert.deepEqual(v.rows, []);
  assert.match(v.why, /does not run ship-fleet/);
  assert.ok(!v.why.includes("\n"));
  assert.ok(v.local.some((l) => /trusted as laptop/.test(l.text)));
  assert.ok(v.local.some((l) => /certificate ends 2026-01-12 09:00 UTC \(in 7 days\)/.test(l.text)));
  assert.match(trustJob(s.fleet.state, FORM).errors[0], /Say where FleetLedger is/);
});

test("a key that may only fetch this machine's bundle is pull-only too, and never asked with again", async () => {
  const bundle = example("machine-bundle", names("machine-bundle")[0]);
  const s = await opened({ answers: { ls: ok(bundle) } });
  assert.deepEqual(verbs(s.calls), ["ls"]);
  assert.match(s.fleet.state.ca.pullOnly, /may only fetch this machine's bundle/);
});

test("this computer is marked; each row says its scope, logins, certificate, refresh and key", async () => {
  const doc = example("fleetledger-ca", "fleet.json");
  doc.machines[0].bundle_key = "installed";
  doc.machines[1].bundle_key = "own-line";
  const s = await opened({ answers: { ca: () => ok(doc) }, files: { [CONF]: "name=desk\n" } });
  const rows = machineRows(s.fleet.state, NOW);
  assert.deepEqual(rows.map((r) => [r.name, r.mine, r.revoked]), [
    ["desk", true, false],
    ["fleetledger", false, false],
    ["old-laptop", false, true],
  ]);
  const [desk, own, old] = rows;
  assert.equal(desk.scope, "tag:work");
  assert.equal(desk.logins, "agent");
  assert.equal(desk.cert.tone, "bad");
  assert.match(desk.cert.text, /^lapsed, ended 2026-01-01 03:00 UTC/);
  assert.match(desk.refreshed, /^asked 2025-12-28 09:00 UTC/);
  assert.deepEqual(desk.refresh, { text: "refresh allowed", tone: "good" });
  assert.equal(desk.key.text, "bundle only");
  assert.equal(own.key.text, "full key");
  assert.match(own.refresh.text, /renews its own/);
  assert.equal(own.logins, "agent + owner");
  assert.equal(old.key, null);
  assert.match(old.refresh.text, /^untrusted/);
  doc.machines[0].idle_refused = "2026-01-04T00:00:00Z";
  doc.machines[0].idle_limit = true;
  doc.machines[1].idle_limit = false;
  await s.fleet.loadCa();
  const again = machineRows(s.fleet.state, NOW);
  assert.equal(again[0].refresh.tone, "bad");
  assert.match(again[0].refresh.text, /refused: idle/);
});

test("this computer's name not in the list is said", async () => {
  const s = await opened({ files: { [CONF]: "name=old-laptop\n" } });
  const v = machinesView(s.fleet.state, NOW);
  assert.ok(v.notices.some((n) => /refreshes as old-laptop, which is not a trusted machine/.test(n.text)));
  assert.ok(!v.rows.some((r) => r.mine));
});

// ---- Refresh now ------------------------------------------------------------------------------

test("Refresh now runs the refresher and reads state.json again", async () => {
  const failed = text("refresh-state", "failed.json");
  const okState = text("refresh-state", "ok.json");
  const s = await opened({ files: { [STATE]: failed }, refresher: { code: 0, stdout: "", stderr: "" }, after: { [STATE]: okState } });
  assert.equal(s.fleet.state.local.installed, true);
  assert.match(s.fleet.state.local.refresh.lastError, /refused this machine's bundle/);
  s.calls.length = 0;
  await s.fleet.refreshNow();
  const run = s.calls.find((c) => c.command === REFRESHER);
  assert.deepEqual(run.args, []);
  assert.equal(run.options.timeout, REFRESHER_MS);
  assert.equal(s.fleet.state.local.refresh.lastError, null);
  assert.equal(s.fleet.state.refreshNote.tone, "good");
  assert.equal(s.fleet.state.refreshing, false);
});

test("Refresh now that fails says its first line on stderr", async () => {
  const s = await opened({ refresher: { code: 1, stdout: "", stderr: "\nFleetLedger could not be reached.\nmore\n" } });
  await s.fleet.refreshNow();
  assert.deepEqual(s.fleet.state.refreshNote, { tone: "bad", text: `${REFRESHER}: FleetLedger could not be reached.` });
});

test("no refresher: Refresh now says so and how to get one", async () => {
  const s = await opened();
  assert.equal(s.fleet.state.local.installed, false);
  const v = machinesView(s.fleet.state, NOW);
  assert.equal(v.canRefresh, false);
  assert.ok(v.local.some((l) => l.text === NO_REFRESHER));
  assert.match(NO_REFRESHER, /Trust a machine for this one/);
  assert.match(NO_REFRESHER, /paste here, in a terminal, the line ship-fleet trust prints/);
  await s.fleet.refreshNow();
  assert.deepEqual(s.fleet.state.refreshNote, { tone: "warn", text: NO_REFRESHER });
  assert.ok(!s.calls.some((c) => c.command === REFRESHER));
});

test("the header: this computer's certificate and last refresh, and a failed refresh", async () => {
  const s = stand({ files: { [STATE]: text("refresh-state", "failed.json") } });
  await s.fleet.refresh();
  const h = header(s.fleet.state, NOW, { settings: SETTINGS });
  assert.ok(h.facts.includes("this computer: certificate ends in 7 days, refreshed 1 h ago (last try failed)"));
  assert.ok(h.notices.some((n) => n.tone === "warn" && /^This computer's last refresh failed: FleetLedger refused/.test(n.text)));
  const none = stand();
  await none.fleet.refresh();
  assert.equal(localFact(none.fleet.state, NOW), null); // no state.json: nothing said
  const bad = stand({ files: { [STATE]: text("refresh-state", "invalid/machine-not-a-name.json") } });
  await bad.fleet.refresh();
  assert.ok(header(bad.fleet.state, NOW, { settings: SETTINGS }).notices.some((n) => /state\.json: "machine"/.test(n.text)));
});

test("after a trust job ends, the list is read again once", async () => {
  const s = await opened();
  const plan = trustJob(s.fleet.state, FORM);
  const LOCAL = { id: "s1", machine: null, tabs: [{ id: "t1", active: true }] };
  assert.deepEqual(s.fleet.startJob(plan.job, { panes: [], spaces: [LOCAL] }), { ok: true });
  const term = (state) => ({ address: "t9", argv: plan.job.argv, state });
  await s.fleet.checkJobs([term("live")]);
  s.calls.length = 0;
  await s.fleet.checkJobs([term("exited")]);
  assert.equal(verbs(s.calls).filter((v) => v === "ca").length, 1);
});

test("an impossible date in state.json is refused, not shown as healthy", () => {
  const ok = example("refresh-state", "ok.json");
  assert.match(readRefreshState(JSON.stringify({ ...ok, certificate_valid_before: "2026-99-99T09:00:00Z" })).error, /certificate_valid_before/);
});

test("another FleetLedger or key in settings.json drops the list read from the last one", async () => {
  const s = await opened();
  assert.equal(s.fleet.state.ca.machines.length, 3);
  s.disk[SETTINGS] = JSON.stringify({ ssh: "fleetledger@other.example.invalid" });
  await s.fleet.refresh();
  assert.equal(s.fleet.state.ca, null);
  assert.match(machinesView(s.fleet.state, NOW).why, /not read yet: Reload/);
  assert.match(trustJob(s.fleet.state, FORM).errors[0], /have not been read/);
});

test("a trust job lost from sight still brings one read of the list", async () => {
  const s = await opened();
  const plan = trustJob(s.fleet.state, FORM);
  const LOCAL = { id: "s1", machine: null, tabs: [{ id: "t1", active: true }] };
  s.fleet.startJob(plan.job, { panes: [], spaces: [LOCAL] });
  s.fleet.state.jobs[0].startedAt = NOW - 10 * 60 * 1000; // past LOST_MS, never seen
  s.calls.length = 0;
  await s.fleet.checkJobs([]);
  assert.equal(verbs(s.calls).filter((v) => v === "ca").length, 1);
});

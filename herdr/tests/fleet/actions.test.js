// The Fleet panel's actions (herdr/mods/fleet/model.js): every command it can open a terminal
// for, which ones it offers in each documented state, and what it does when a job's terminal
// ends. With a stand-in for Herdr; the documents it reads are the repo's own examples.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import {
  BY_STATE,
  Fleet,
  LOST_MS,
  REFRESHER_MS,
  actionGate,
  checkRepo,
  checkSize,
  checkSnapshot,
  checkTag,
  fleetLedgerMenu,
  job,
  jobPlan,
  knownTags,
  newShipJob,
  olderThan,
  readAccounts,
  readSignins,
  runsJob,
  shellLine,
  shellWord,
  shipJob,
  shipMenu,
  SCHEMA,
} from "../../mods/fleet/model.js";

const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const EXAMPLES = join(ROOT, "contracts", "examples");
const example = (kind, name) => JSON.parse(readFileSync(join(EXAMPLES, kind, name), "utf8"));
const examples = (kind) =>
  readdirSync(join(EXAMPLES, kind))
    .filter((f) => f.endsWith(".json"))
    .sort()
    .map((f) => example(kind, f));

const HOME = "/home/agent";
const APP = `${HOME}/.config/herdr-app`;
const SETTINGS = `${APP}/mods/fleet/settings.json`;
const REFRESHER = `${HOME}/.ssh/shipshape/refresh`;
const DEST = "fleetledger@fleetledger.example.invalid";
const WHERE = { ssh: DEST, identity: null };
const SKIPPING = ["--yes", "-y", "--force", "--revoke-my-key", "--stdin"];

// ---- the stand-in ---------------------------------------------------------------------------

const ok = (doc) => ({ code: 0, stdout: `${JSON.stringify(doc)}\n`, stderr: "" });

/** A Herdr stand-in. `answers` maps a ship-fleet verb (or "signins --accounts") to a result;
 *  `refresher`: null (none), or what it answers. */
function stand({ about = example("fleetledger-about", "desktop-checked.json"), ships = examples("fleetledger-ship"), attention = [], answers = {}, refresher = null, clock } = {}) {
  const calls = [];
  const runs = [];
  const toasts = [];
  const all = {
    ls: ok(ships),
    attention: ok(attention),
    about: ok(about),
    signins: ok(examples("fleetledger-signin")),
    "signins --accounts": ok(example("fleetledger-tag-accounts", "fleet.json")),
    ...answers,
  };
  const host = {
    home: () => HOME,
    appHome: () => APP,
    readTextFile: async (path) => (path === SETTINGS ? JSON.stringify({ ssh: DEST }) : null),
    exec: async (command, args, options) => {
      calls.push({ command, args, options });
      if (command === "ssh") {
        const line = args.at(-1);
        const verb = /^'ship-fleet' '([a-z-]+)'/.exec(line)?.[1];
        const key = verb === "signins" && line.includes("'--accounts'") ? "signins --accounts" : verb;
        const a = all[key];
        if (a === undefined) throw new Error(`no answer for ${line}`);
        return typeof a === "function" ? a(args) : a;
      }
      if (command === "test") return { code: refresher ? 0 : 1, stdout: "", stderr: "" };
      if (command === REFRESHER) return refresher;
      if (command === "xdg-open") return { code: 0, stdout: "", stderr: "" };
      throw new Error(`unexpected ${command}`);
    },
    hc: async () => ({ code: 0, stdout: JSON.stringify({ machines: [] }), stderr: "" }),
    run: (name, args) => {
      runs.push([name, args]);
      return null;
    },
    notify: (n) => {
      toasts.push(n);
      return "toast-1";
    },
  };
  const now = { t: 1_000_000 };
  return { host, calls, runs, toasts, now, fleet: new Fleet(host, clock ?? (() => now.t)) };
}

async function readFleet(opts) {
  const s = stand(opts);
  await s.fleet.refresh();
  return s;
}

/** What `sh` makes of a line: its words, as a program would get them. */
function shWords(line) {
  const out = execFileSync("sh", ["-c", `printf '%s\\0' ${line}`], { encoding: "utf8" });
  return out.split("\0").slice(0, -1);
}

// ---- building commands ----------------------------------------------------------------------

test("a job: one ssh with a terminal, the ship-fleet line quoted, and shown exactly as run", () => {
  const j = job(WHERE, "destroy ship-01", [["destroy", "ship-01"]]);
  assert.deepEqual(j.argv, ["ssh", "-t", "-o", "ConnectTimeout=8", "--", DEST, "'ship-fleet' 'destroy' 'ship-01'"]);
  assert.equal(j.shown, `ssh -t -o ConnectTimeout=8 -- ${DEST} "'ship-fleet' 'destroy' 'ship-01'"`);
  assert.deepEqual(shWords(j.shown), j.argv); // what is shown, pasted into a shell, is what runs
  assert.deepEqual(shWords(j.remote), ["ship-fleet", "destroy", "ship-01"]); // and what FleetLedger's shell runs
  assert.ok(!j.argv.includes("BatchMode=yes")); // ssh may ask for a key's passphrase in the terminal
});

test("a job with a key file: -i and IdentitiesOnly, and a path with a space still round-trips", () => {
  const j = job({ ssh: DEST, identity: "/home/agent/my keys/fleet" }, "doctor", [["doctor"]]);
  assert.deepEqual(j.argv.slice(0, 8), ["ssh", "-t", "-o", "ConnectTimeout=8", "-i", "/home/agent/my keys/fleet", "-o", "IdentitiesOnly=yes"]);
  assert.deepEqual(shWords(j.shown), j.argv);
});

test("shellWord: bare when safe, quoted when not, and sh reads back every one", () => {
  const values = ["ship-01", "a b", "it's", 'say "hi"', "$HOME", "`x`", "back\\slash", "!bang", "", "'ship-fleet' 'new' 'x' && 'ship-fleet' 'login' 'x' 'claude'"];
  for (const v of values) assert.deepEqual(shWords(shellWord(v)), [v], v);
  assert.equal(shellWord("ship-01"), "ship-01");
  assert.deepEqual(shWords(shellLine(values)), values);
  assert.throws(() => shellWord("a\nb"), /line break/);
});

test("the panel never sends a flag that skips a question or a check", () => {
  for (const flag of SKIPPING) {
    assert.throws(() => job(WHERE, "x", [["destroy", "ship-01", flag]]), /never skips/, flag);
    assert.throws(() => job(WHERE, "x", [["update", "ship-01"], ["reboot", "ship-01", `${flag}=1`]]), /never skips/, flag);
  }
});

test("each Ship action's exact command", () => {
  const remote = (id, value) => shWords(shipJob(WHERE, id, "ship-01", value).remote);
  assert.deepEqual(remote("signin", "claude"), ["ship-fleet", "login", "ship-01", "claude"]);
  assert.deepEqual(remote("signin", "gcloud-adc"), ["ship-fleet", "login", "ship-01", "gcloud-adc"]);
  assert.deepEqual(remote("update"), ["ship-fleet", "update", "ship-01"]);
  assert.deepEqual(remote("reboot"), ["ship-fleet", "reboot", "ship-01"]);
  assert.deepEqual(remote("snapshots"), ["ship-fleet", "snapshot", "ls", "ship-01"]);
  assert.deepEqual(remote("snapshot"), ["ship-fleet", "snapshot", "take", "ship-01"]);
  assert.deepEqual(remote("rollback", "before-upgrade"), ["ship-fleet", "snapshot", "rollback", "ship-01", "before-upgrade"]);
  assert.deepEqual(remote("destroy"), ["ship-fleet", "destroy", "ship-01"]);
  assert.deepEqual(remote("finish"), ["ship-fleet", "status", "ship-01", "--wait"]);
  assert.equal(shipJob(WHERE, "destroy", "ship-01").shown, `ssh -t -o ConnectTimeout=8 -- ${DEST} "'ship-fleet' 'destroy' 'ship-01'"`);
});

test("names, services and snapshots ship-fleet would refuse are refused, and nothing is built", () => {
  for (const name of ["Ship-01", "ship_01", "-rf", "a b", "../x", "ship-01;reboot", "", "x".repeat(32)]) {
    assert.throws(() => shipJob(WHERE, "destroy", name), /not a Ship name/, name);
  }
  for (const service of ["Claude", "claude;id", "", "-y", "a b"]) assert.throws(() => shipJob(WHERE, "signin", "ship-01", service), /not a service/, service);
  for (const snap of ["current", "Current", "1snap", "a", "snap shot", "snap'x", "x".repeat(41)]) assert.throws(() => checkSnapshot(snap), /not a snapshot name/, snap);
  assert.equal(checkSnapshot(" snap-20260101-000000 "), "snap-20260101-000000");
});

test("typed tags, repositories and sizes: ship-fleet's own limits", () => {
  assert.equal(checkTag(" work "), "work");
  for (const tag of ["Work", "-work", "wo rk", "work,home", "", "x".repeat(32), "wörk"]) assert.throws(() => checkTag(tag), /not a tag/, tag);
  assert.equal(checkRepo("example-user/agent-setup"), "example-user/agent-setup");
  for (const repo of ["agent-setup", "a/b/c", "https://github.com/a/b", "a/..", "-a/b", "a/b c", "a/b;id"]) assert.throws(() => checkRepo(repo), /not a GitHub repository/, repo);
  assert.equal(checkSize("cores", ""), null);
  assert.equal(checkSize("cores", "08"), "8");
  assert.equal(checkSize("memory_mb", "16384"), "16384");
  for (const [k, v] of [["cores", "0"], ["cores", "65"], ["cores", "2.5"], ["memory_mb", "4095"], ["disk_gb", "19"], ["disk_gb", "1e3"], ["cores", "-1"]]) {
    assert.throws(() => checkSize(k, v), /a whole number/, `${k}=${v}`);
  }
});

// ---- New Ship -------------------------------------------------------------------------------

test("New Ship: one line, new then the Claude sign-in, every value quoted", async () => {
  const { fleet } = await readFleet();
  await fleet.loadAccounts();
  const form = { name: "ship-07", tag: "work", repo: "example-user/agent-setup", cores: "8", memory_mb: "16384", disk_gb: "80", signins: null };
  const plan = newShipJob(fleet.state, form);
  assert.deepEqual(plan.errors, []);
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' '--tag' 'work' '--repo' 'example-user/agent-setup' '--cores' '8' '--memory-mb' '16384' '--disk-gb' '80' && 'ship-fleet' 'login' 'ship-07' 'claude'");
  assert.deepEqual(shWords(plan.job.shown), plan.job.argv);
  assert.equal(plan.job.argv[1], "-t");
  assert.equal(plan.job.ship, "ship-07");
  assert.equal(plan.job.kind, "new");
});

test("New Ship: only a name, and the defaults are FleetLedger's", async () => {
  const { fleet } = await readFleet();
  const plan = newShipJob(fleet.state, { name: "ship-07", signins: null });
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' && 'ship-fleet' 'login' 'ship-07' 'claude'");
});

test("New Ship: sign-ins chosen from FleetLedger's list; without Claude, no Claude sign-in", async () => {
  const { fleet } = await readFleet();
  await fleet.loadAccounts();
  assert.deepEqual(fleet.state.accounts.signins, ["claude", "gcloud", "github"]);
  let plan = newShipJob(fleet.state, { name: "ship-07", signins: ["github", "claude"] });
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' '--signins' 'claude,github' && 'ship-fleet' 'login' 'ship-07' 'claude'");
  plan = newShipJob(fleet.state, { name: "ship-07", signins: ["github"] });
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' '--signins' 'github'");
  plan = newShipJob(fleet.state, { name: "ship-07", signins: [] });
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' '--signins' ''");
  plan = newShipJob(fleet.state, { name: "ship-07", signins: ["claude", "gcloud", "github"] });
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' && 'ship-fleet' 'login' 'ship-07' 'claude'");
  plan = newShipJob(fleet.state, { name: "ship-07", signins: ["claude", "token"] });
  assert.match(plan.errors.join(" "), /Not a sign-in FleetLedger offers: token/);
});

test("New Ship: what is wrong is said, and nothing is built", async () => {
  const { fleet } = await readFleet();
  const bad = newShipJob(fleet.state, { name: "Ship 7", tag: "Work!", repo: "nope", cores: "0", memory_mb: "lots", disk_gb: "", signins: ["claude"] });
  assert.equal(bad.job, undefined);
  assert.equal(bad.errors.length, 6);
  assert.match(bad.errors.join("\n"), /not a Ship name[\s\S]*not a tag[\s\S]*not a GitHub repository[\s\S]*cores[\s\S]*memory mb[\s\S]*sign-ins FleetLedger offers are not known/);
  assert.deepEqual(newShipJob(fleet.state, { name: "" }).errors, ["Give it a name."]);
  assert.deepEqual(newShipJob(fleet.state, { name: "ship-01" }).errors, ["ship-01 is already a Ship."]);
});

test("New Ship: the tags to choose from are the fleet's", async () => {
  const { fleet } = await readFleet();
  assert.deepEqual(knownTags(fleet.state), ["personal", "work"]); // the Ships' own
  await fleet.loadAccounts();
  assert.deepEqual(knownTags(fleet.state), ["lab", "personal", "work"]); // and those that name accounts
  const s = stand({ answers: { "signins --accounts": ok({ ...example("fleetledger-tag-accounts", "fleet.json"), tags: [{ tag: "home", accounts: {}, ships: [] }, { tag: "Bad Tag" }] }) } });
  await s.fleet.refresh();
  await s.fleet.loadAccounts();
  assert.deepEqual(knownTags(s.fleet.state), ["home", "personal", "work"]);
});

// ---- what is offered ------------------------------------------------------------------------

test("actionGate: settings, an older FleetLedger, a locked vault, a copy on standby, or nothing", async () => {
  const fresh = new Fleet(stand().host);
  assert.match(actionGate(fresh.state), /Say where FleetLedger is/);
  assert.equal(actionGate((await readFleet()).fleet.state), null);
  assert.match(actionGate((await readFleet({ about: example("fleetledger-about", "vm-locked.json") })).fleet.state), /vault is locked.*Unlock/);
  assert.match(actionGate((await readFleet({ about: example("fleetledger-about", "degraded.json") })).fleet.state), /acts on no Ship \(retired/);
  const noAbout = await readFleet({ answers: { about: { code: 2, stdout: "", stderr: "usage: ship-fleet\nship-fleet: error: argument command: invalid choice: 'about'\n" } } });
  assert.match(actionGate(noAbout.fleet.state), /older than the panel's actions need/);
  const oldLs = await readFleet({ ships: examples("fleetledger-ship").map((s) => ({ ...s, schema: undefined })) });
  assert.equal(oldLs.fleet.state.older, true);
  assert.match(actionGate(oldLs.fleet.state), /older than the panel's actions need/);
  const unreachable = await readFleet({ answers: { about: { code: 1, stdout: "", stderr: "Traceback\n" } } });
  assert.match(actionGate(unreachable.fleet.state), /state is not known.*Refresh/);
});

test("olderThan: no schema, or the same shape at a lower version", () => {
  assert.equal(olderThan({}, SCHEMA.ship), true);
  assert.equal(olderThan({ schema: "shipshape.fleetledger-ship/0" }, SCHEMA.ship), true);
  assert.equal(olderThan({ schema: SCHEMA.ship }, SCHEMA.ship), false);
  assert.equal(olderThan({ schema: "shipshape.fleetledger-ship/2" }, SCHEMA.ship), false);
  assert.equal(olderThan({ schema: "something/0" }, SCHEMA.ship), false);
});

const offered = (menu) => menu.items.filter((i) => i.job).map((i) => i.id);

test("which Ship actions each documented state offers", async () => {
  const { fleet } = await readFleet();
  const ship = (name) => fleet.state.ships.find((s) => s.name === name);
  for (const s of fleet.state.ships) await fleet.loadSignins(s.name);
  // ship-01, needs-sign-in and answering: everything, a sign-in per service it uses
  assert.deepEqual(offered(shipMenu(fleet.state, ship("ship-01"))), ["signin:claude", "update", "reboot", "snapshots", "snapshot", "destroy"]);
  assert.deepEqual(offered(shipMenu(fleet.state, ship("ship-01"), "before-upgrade")), ["signin:claude", "update", "reboot", "snapshots", "snapshot", "rollback", "destroy"]);
  // ship-02, unreachable: only through the hypervisor
  const two = shipMenu(fleet.state, ship("ship-02"), "before-upgrade");
  assert.deepEqual(offered(two), ["snapshots", "snapshot", "rollback", "destroy"]);
  assert.match(two.items.find((i) => i.id === "signin").why, /unreachable/);
  // ship-04, provisioning: let it finish, or destroy it
  assert.deepEqual(offered(shipMenu(fleet.state, ship("ship-04"))), ["finish", "destroy"]);
  // every documented state has a row in BY_STATE, and only not-enrolled and provisioning finish
  const schema = JSON.parse(readFileSync(join(ROOT, "contracts", "fleetledger-ship.schema.json"), "utf8"));
  assert.deepEqual(Object.keys(BY_STATE).sort(), [...schema.properties.state.enum].sort());
  for (const [state, ids] of Object.entries(BY_STATE)) {
    assert.equal(ids.includes("signin"), ["ready", "running", "needs-sign-in", "unknown"].includes(state), state);
    assert.equal(ids.includes("finish"), ["provisioning", "not-enrolled"].includes(state), state);
    assert.ok(ids.includes("destroy"), state);
  }
  const notEnrolled = { name: "ship-09", tags: [], state: "not-enrolled", problem: null };
  assert.deepEqual(offered(shipMenu(fleet.state, notEnrolled)), ["finish", "destroy"]);
  assert.ok(!shipMenu(fleet.state, notEnrolled).items.some((i) => i.id.startsWith("signin") && i.job));
  const future = shipMenu(fleet.state, { name: "ship-09", tags: [], state: "sailing", problem: null });
  assert.match(future.why, /new to this panel/);
  assert.deepEqual(offered(future), ["snapshots", "snapshot", "destroy"]);
});

test("rollback needs a snapshot's name, and refuses one ship-fleet would", async () => {
  const { fleet } = await readFleet();
  const one = fleet.state.ships.find((s) => s.name === "ship-01");
  assert.match(shipMenu(fleet.state, one, "").items.find((i) => i.id === "rollback").why, /type the snapshot's name/);
  assert.match(shipMenu(fleet.state, one, "current").items.find((i) => i.id === "rollback").why, /not a snapshot name/);
  const r = shipMenu(fleet.state, one, "snap-1").items.find((i) => i.id === "rollback");
  assert.equal(r.job.remote, "'ship-fleet' 'snapshot' 'rollback' 'ship-01' 'snap-1'");
  assert.match(r.note, /asks you to type yes in the terminal/);
});

test("sign-ins: one per service the Ship uses, none it has off, none while it does not answer", async () => {
  const rows = [
    { ...example("fleetledger-signin", "healthy.json"), ship: "ship-01", service: "claude", provider: "claude", group: "healthy" },
    { ...example("fleetledger-signin", "healthy.json"), ship: "ship-01", service: "gcloud", provider: "gcloud", group: "off" },
    { ...example("fleetledger-signin", "healthy.json"), ship: "ship-01", service: "gcloud_adc", provider: "gcloud-adc", group: "action" },
    { ...example("fleetledger-signin", "healthy.json"), ship: "ship-01", service: "github", provider: "github", group: "action", answers: false },
    { ...example("fleetledger-signin", "healthy.json"), ship: "ship-02", service: "claude", provider: "claude" },
  ];
  const { fleet, calls } = await readFleet({ answers: { signins: ok(rows) } });
  await fleet.loadSignins("ship-01");
  assert.equal(calls.at(-1).args.at(-1), "'ship-fleet' 'signins' 'ship-01' '--offline' '--json'");
  const m = shipMenu(fleet.state, fleet.state.ships.find((s) => s.name === "ship-01"));
  const signins = m.items.filter((i) => i.id.startsWith("signin"));
  assert.deepEqual(signins.map((i) => [i.id, Boolean(i.job)]), [["signin:claude", true], ["signin:gcloud-adc", true], ["signin:github", false]]);
  assert.match(signins[2].why, /has not answered/);
  assert.deepEqual(shWords(signins[1].job.remote), ["ship-fleet", "login", "ship-01", "gcloud-adc"]);
  const failed = await readFleet({ answers: { signins: { code: 1, stdout: "", stderr: "no such Ship\n" } } });
  await failed.fleet.loadSignins("ship-01");
  assert.match(shipMenu(failed.fleet.state, failed.fleet.state.ships[0]).items.find((i) => i.id === "signin").why, /no such Ship/);
});

test("readSignins and readAccounts read the contracts' examples", () => {
  const all = examples("fleetledger-signin");
  const got = readSignins(JSON.stringify(all), "ship-01");
  assert.equal(got.error, null);
  assert.deepEqual(got.rows.map((r) => r.provider), all.filter((r) => r.ship === "ship-01").map((r) => r.provider));
  assert.match(readSignins(JSON.stringify([{ ship: "ship-01" }]), "ship-01").error, /older than the contract/);
  const acc = readAccounts(JSON.stringify(example("fleetledger-tag-accounts", "fleet.json")));
  assert.deepEqual(acc, { signins: ["claude", "gcloud", "github"], tags: ["lab", "personal", "work"] });
  assert.match(readAccounts("{}").error, /older than the contract/);
});

test("the FleetLedger menu: Unlock only while locked; Doctor; Self-update; the web UI here", async () => {
  const unlocked = fleetLedgerMenu((await readFleet()).fleet.state);
  assert.deepEqual(offered(unlocked), ["self-update", "doctor"]);
  assert.match(unlocked.items.find((i) => i.id === "web-ui").why, /not on for other devices/);
  const lockedState = (await readFleet({ about: example("fleetledger-about", "vm-locked.json") })).fleet.state;
  const locked = fleetLedgerMenu(lockedState);
  assert.deepEqual(offered(locked), ["unlock", "self-update", "doctor"]);
  const unlock = locked.items.find((i) => i.id === "unlock");
  assert.equal(unlock.job.shown, `ssh -t -o ConnectTimeout=8 -- ${DEST} "'ship-fleet' 'unlock'"`);
  assert.match(unlock.note, /passphrase in the terminal, never here/);
  assert.equal(locked.items.find((i) => i.id === "web-ui").open, "https://fleetledger.example-tailnet.ts.net");
  assert.deepEqual(shWords(locked.items.find((i) => i.id === "self-update").job.remote), ["ship-fleet", "self-update"]);
  assert.deepEqual(shWords(locked.items.find((i) => i.id === "doctor").job.remote), ["ship-fleet", "doctor"]);
  const retired = fleetLedgerMenu((await readFleet({ about: example("fleetledger-about", "degraded.json") })).fleet.state);
  assert.deepEqual(offered(retired), ["doctor"]);
  assert.match(fleetLedgerMenu(new Fleet(stand().host).state).why, /Say where FleetLedger is/);
});

test("the web UI opens with the local opener, no ssh, and only an https address", async () => {
  const { fleet, calls } = await readFleet({ about: example("fleetledger-about", "vm-locked.json") });
  const before = calls.length;
  assert.deepEqual(await fleet.openWebUi("https://fleetledger.example-tailnet.ts.net"), { ok: true });
  assert.deepEqual(calls.slice(before).map((c) => [c.command, c.args]), [["xdg-open", ["https://fleetledger.example-tailnet.ts.net"]]]);
  for (const url of ["http://fleetledger.example.invalid", "file:///etc/passwd", "https://a b", "--help"]) assert.match((await fleet.openWebUi(url)).error, /Not a web UI address/);
  assert.equal(calls.length, before + 1);
});

test("every command any menu or form can build skips no question", async () => {
  const states = [example("fleetledger-about", "desktop-checked.json"), example("fleetledger-about", "vm-locked.json")];
  const built = [];
  for (const about of states) {
    const { fleet } = await readFleet({ about });
    await fleet.loadAccounts();
    for (const s of fleet.state.ships) {
      await fleet.loadSignins(s.name);
      for (const state of Object.keys(BY_STATE)) built.push(...shipMenu(fleet.state, { ...s, state, problem: null }, "snap-1").items);
    }
    built.push(...fleetLedgerMenu(fleet.state).items);
    const plan = newShipJob(fleet.state, { name: "ship-07", tag: "work", repo: "a/b", cores: "4", memory_mb: "8192", disk_gb: "40", signins: ["claude"] });
    if (plan.job) built.push(plan);
  }
  const jobs = built.filter((i) => i.job).map((i) => i.job);
  assert.ok(jobs.length > 40);
  for (const j of jobs) {
    const words = [...j.argv, ...j.remote.split(" && ").flatMap(shWords)];
    for (const flag of SKIPPING) assert.ok(!words.some((w) => w === flag || w.startsWith(`${flag}=`)), `${flag} in ${j.shown}`);
    assert.deepEqual(shWords(j.shown), j.argv);
  }
});

// ---- jobs -----------------------------------------------------------------------------------

const LOCAL = { id: "s1", machine: null, tabs: [{ id: "t1", active: true }] };
const ON_SHIP = { id: "s2", machine: "ship-01", tabs: [{ id: "t2", active: true }] };

test("a job's tab opens on this computer, never in a Ship's space", () => {
  const j = job(WHERE, "update ship-01", [["update", "ship-01"]]);
  const fleetTab = [{ pane: "p1", mod: "fleet", terminal: null, tab: "t1" }];
  assert.deepEqual(jobPlan(j, fleetTab, [ON_SHIP, LOCAL]), ["tab.new", { name: "update ship-01", space: "s1", argv: j.argv }]);
  const inShip = [{ pane: "p2", mod: "fleet", terminal: null, tab: "t2" }];
  assert.deepEqual(jobPlan(j, inShip, [ON_SHIP, LOCAL]), ["tab.new", { name: "update ship-01", space: "s1", argv: j.argv }]);
  assert.deepEqual(jobPlan(j, inShip, [ON_SHIP]), ["space.new", { name: "Fleet jobs", tab: "update ship-01", argv: j.argv }]);
});

test("runsJob: its argv, or a shell running its line", () => {
  const j = job(WHERE, "update ship-01", [["update", "ship-01"]]);
  assert.ok(runsJob({ argv: j.argv }, j));
  assert.ok(runsJob({ argv: ["zsh", "-c", j.shown] }, j));
  assert.ok(!runsJob({ argv: ["ssh", "-t", "--", DEST, "'ship-fleet' 'update' 'ship-02'"] }, j));
  assert.ok(!runsJob({}, j));
});

function term(j, state = "live", address = "t9") {
  return { address, argv: j.argv, state };
}

test("a New Ship job: when its terminal ends, the refresher, one read, and a toast once it is ready", async () => {
  const ready = { ...example("fleetledger-ship", "answering.json"), name: "ship-07", state: "ready", attention: [] };
  const s = stand({ refresher: { code: 0, stdout: "", stderr: "" } });
  await s.fleet.refresh();
  const plan = newShipJob(s.fleet.state, { name: "ship-07", signins: null });
  assert.deepEqual(s.fleet.startJob(plan.job, { panes: [], spaces: [LOCAL] }), { ok: true });
  assert.deepEqual(s.runs, [["tab.new", { name: "new ship-07", space: "s1", argv: plan.job.argv }]]);
  assert.deepEqual(await s.fleet.checkJobs([term(plan.job)]), []); // running
  assert.equal(s.fleet.state.jobs[0].address, "t9");
  s.calls.length = 0;
  s.now.t += POLL_GAP;
  const ships = [...examples("fleetledger-ship"), ready];
  s.host.exec = wrapLs(s, ships);
  const ended = await s.fleet.checkJobs([term(plan.job, "exited")]);
  assert.equal(ended.length, 1);
  assert.deepEqual(s.fleet.state.jobs, []);
  const order = s.calls.map((c) => (c.command === "ssh" ? /'ship-fleet' '([a-z-]+)'/.exec(c.args.at(-1))[1] : c.command));
  assert.deepEqual(order.slice(0, 2), ["test", REFRESHER]); // the refresher first, so hc knows the Ship
  assert.equal(s.calls[1].options.timeout, REFRESHER_MS);
  assert.ok(order.includes("ls"));
  assert.ok(s.calls.filter((c) => c.command === "ssh").every((c) => c.args.includes("BatchMode=yes"))); // reads, as F1's
  assert.ok(s.calls.filter((c) => c.command === "ssh").every((c) => !/'(new|login|destroy|update|reboot)'/.test(c.args.at(-1))));
  assert.deepEqual(s.toasts, [{ title: "ship-07 is ready", body: "A new Ship, from the Fleet panel.", level: "success" }]);
  assert.equal(s.fleet.state.jobNote, null);
});

const POLL_GAP = 1000;
/** The stand-in, answering ls with these Ships from now on. */
function wrapLs(s, ships) {
  const exec = s.host.exec;
  return async (command, args, options) => {
    if (command !== "ssh" || !/^'ship-fleet' 'ls'/.test(args.at(-1))) return exec(command, args, options);
    s.calls.push({ command, args, options });
    return ok(ships);
  };
}

test("a job whose terminal is gone counts as ended; no refresher is normal and silent", async () => {
  const s = await readFleet();
  const j = shipJob(WHERE, "update", "ship-01");
  s.fleet.startJob(j, { panes: [], spaces: [LOCAL] });
  await s.fleet.checkJobs([term(j)]);
  const ended = await s.fleet.checkJobs([]);
  assert.equal(ended.length, 1);
  assert.equal(s.fleet.state.jobNote, null);
  assert.deepEqual(s.toasts, []);
  assert.ok(!s.calls.some((c) => c.command === REFRESHER));
});

test("a New Ship that is not ready yet: no toast, its state said", async () => {
  const s = await readFleet();
  const plan = newShipJob(s.fleet.state, { name: "ship-07", signins: null });
  s.fleet.startJob(plan.job, { panes: [], spaces: [LOCAL] });
  await s.fleet.checkJobs([term(plan.job)]);
  s.host.exec = wrapLs(s, [...examples("fleetledger-ship"), { ...example("fleetledger-ship", "answering.json"), name: "ship-07" }]);
  await s.fleet.checkJobs([]);
  assert.deepEqual(s.toasts, []);
  assert.match(s.fleet.state.jobNote, /^ship-07: needs-sign-in/);
});

test("a refresher that fails is said, with its first line", async () => {
  const s = await readFleet({ refresher: { code: 1, stdout: "", stderr: "\nno key\nmore\n" } });
  const j = shipJob(WHERE, "destroy", "ship-01");
  s.fleet.startJob(j, { panes: [], spaces: [LOCAL] });
  await s.fleet.checkJobs([term(j)]);
  await s.fleet.checkJobs([term(j, "exited")]);
  assert.equal(s.fleet.state.jobNote, `${REFRESHER}: no key`);
});

test("a job that ended before the panel looked counts as ended; an older terminal is not it", async () => {
  const s = await readFleet();
  const j = shipJob(WHERE, "update", "ship-01");
  s.fleet.startJob(j, { panes: [], spaces: [LOCAL], terminals: [term(j, "exited", "old")] });
  assert.deepEqual(await s.fleet.checkJobs([term(j, "exited", "old")]), []);
  assert.equal((await s.fleet.checkJobs([term(j, "exited", "old"), term(j, "exited", "new")])).length, 1);
  assert.deepEqual(s.fleet.state.jobs, []);
  assert.equal(s.fleet.state.jobNote, null);
});

test("two jobs with the same command each follow their own terminal", async () => {
  const s = await readFleet();
  const j = shipJob(WHERE, "update", "ship-01");
  s.fleet.startJob(j, { panes: [], spaces: [LOCAL] });
  s.fleet.startJob(j, { panes: [], spaces: [LOCAL] });
  await s.fleet.checkJobs([term(j, "live", "a"), term(j, "live", "b")]);
  assert.deepEqual(s.fleet.state.jobs.map((x) => x.address), ["a", "b"]);
  assert.equal((await s.fleet.checkJobs([term(j, "live", "b")])).length, 1);
  assert.deepEqual(s.fleet.state.jobs.map((x) => x.address), ["b"]);
});

test("a job whose terminal never shows up is let go, saying so", async () => {
  const s = await readFleet();
  const j = shipJob(WHERE, "reboot", "ship-01");
  s.fleet.startJob(j, { panes: [], spaces: [LOCAL] });
  assert.deepEqual(await s.fleet.checkJobs([]), []);
  s.now.t += LOST_MS;
  assert.equal((await s.fleet.checkJobs([])).length, 1);
  assert.match(s.fleet.state.jobNote, /Lost sight of reboot ship-01/);
});

test("a tab Herdr will not open is said, and not followed", async () => {
  const s = await readFleet();
  s.host.run = () => {
    throw new Error("layout unavailable");
  };
  assert.deepEqual(s.fleet.startJob(shipJob(WHERE, "update", "ship-01"), { panes: [], spaces: [LOCAL] }), { error: "Couldn't open a terminal: layout unavailable" });
  assert.deepEqual(s.fleet.state.jobs, []);
});

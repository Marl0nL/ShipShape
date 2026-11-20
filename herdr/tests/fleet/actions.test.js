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
  checkFirstAgent,
  checkHost,
  checkSnapshot,
  checkTag,
  checkTimezone,
  checkVmid,
  fleetLedgerMenu,
  job,
  jobPlan,
  knownTags,
  imagesOn,
  newShipChoices,
  newShipJob,
  olderThan,
  readAccounts,
  readDefaults,
  readImages,
  readTools,
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
const SKIPPING = ["--yes", "-y", "--force", "--revoke-my-key", "--stdin", "--code"];
const IMG = "26.04-20261102-3f9a1c2e";
// Proxmox set up, its baked image published there, and new Ships still on the stock template.
const IMAGES = { ...example("fleetledger-images", "pinned-on-proxmox.json"), pinned: "stock" };

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
    tools: ok(example("fleetledger-tools", "with-toolkits.json")),
    image: ok(IMAGES),
    defaults: ok(example("fleetledger-defaults", "catalogue.json")),
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
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' '--tag' 'work' '--repo' 'example-user/agent-setup' '--cores' '8' '--memory-mb' '16384' '--disk-gb' '80' '--autostart' 'main:~/work' && 'ship-fleet' 'login' 'ship-07' 'claude'");
  assert.deepEqual(shWords(plan.job.shown), plan.job.argv);
  assert.equal(plan.job.argv[1], "-t");
  assert.equal(plan.job.ship, "ship-07");
  assert.equal(plan.job.kind, "new");
});

test("New Ship: only a name, and the defaults are FleetLedger's", async () => {
  const { fleet } = await readFleet();
  const plan = newShipJob(fleet.state, { name: "ship-07", signins: null });
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' '--autostart' 'main:~/work' && 'ship-fleet' 'login' 'ship-07' 'claude'");
});

test("New Ship: sign-ins chosen from FleetLedger's list; without Claude, no Claude sign-in", async () => {
  const { fleet } = await readFleet();
  await fleet.loadAccounts();
  assert.deepEqual(fleet.state.accounts.signins, ["claude", "gcloud", "github"]);
  let plan = newShipJob(fleet.state, { name: "ship-07", signins: ["github", "claude"] });
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' '--signins' 'claude,github' '--autostart' 'main:~/work' && 'ship-fleet' 'login' 'ship-07' 'claude'");
  plan = newShipJob(fleet.state, { name: "ship-07", signins: ["github"] });
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' '--signins' 'github' '--autostart' 'main:~/work'");
  plan = newShipJob(fleet.state, { name: "ship-07", signins: [] });
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' '--signins' '' '--autostart' 'main:~/work'");
  plan = newShipJob(fleet.state, { name: "ship-07", signins: ["claude", "gcloud", "github"] });
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' '--autostart' 'main:~/work' && 'ship-fleet' 'login' 'ship-07' 'claude'");
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

test("New Ship: several tags, chosen and typed, each checked and sent once", async () => {
  const { fleet } = await readFleet();
  const plan = newShipJob(fleet.state, { name: "ship-07", tags: ["work", "lab"], tag: "home, work  personal", signins: null, agent_on: false });
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' '--tag' 'work' '--tag' 'lab' '--tag' 'home' '--tag' 'personal' && 'ship-fleet' 'login' 'ship-07' 'claude'");
  const bad = newShipJob(fleet.state, { name: "ship-07", tags: ["work"], tag: "home Bad!", signins: null });
  assert.equal(bad.job, undefined);
  assert.match(bad.errors.join("\n"), /"Bad!" is not a tag/);
});

test("New Ship: the first agent is main in ~/work, as the web form's; named, placed, in a mode, or none", async () => {
  const { fleet } = await readFleet();
  await fleet.loadAccounts();
  const remote = (form) => newShipJob(fleet.state, { name: "ship-07", signins: ["github"], ...form }).job.remote;
  assert.equal(remote({}), "'ship-fleet' 'new' 'ship-07' '--signins' 'github' '--autostart' 'main:~/work'");
  assert.equal(remote({ agent: " lead ", folder: "/srv/work/lead", agent_mode: "plan" }), "'ship-fleet' 'new' 'ship-07' '--signins' 'github' '--autostart' 'lead:/srv/work/lead:plan'");
  assert.equal(remote({ agent_mode: "auto" }), "'ship-fleet' 'new' 'ship-07' '--signins' 'github' '--autostart' 'main:~/work:auto'");
  assert.equal(remote({ agent_on: false, agent: "lead" }), "'ship-fleet' 'new' 'ship-07' '--signins' 'github'");
  // A repository is where the first agent works: `new` clones it into ~/work/NAME for it.
  assert.equal(remote({ repo: "example-user/agent-setup" }), "'ship-fleet' 'new' 'ship-07' '--repo' 'example-user/agent-setup' '--signins' 'github' '--autostart' 'main:~/work'");
  const alone = newShipJob(fleet.state, { name: "ship-07", repo: "example-user/agent-setup", agent_on: false });
  assert.equal(alone.job, undefined);
  assert.match(alone.errors[0], /A repository is for the first agent/);
  assert.equal(checkFirstAgent("", "", ""), "main:~/work");
  assert.equal(checkFirstAgent("agent_2", "~/a folder/x", "bypassPermissions"), "agent_2:~/a folder/x:bypassPermissions");
  for (const [n, f, m, why] of [["Main", "", "", /not an agent name/], ["2nd", "", "", /not an agent name/], ["a:b", "", "", /not an agent name/], ["x".repeat(33), "", "", /not an agent name/], ["", "work", "", /not a folder/], ["", "~work", "", /not a folder/], ["", "~/a:b", "", /not a folder/], ["", "", "yolo", /not a mode/], ["", "", "Plan", /not a mode/]]) {
    assert.throws(() => checkFirstAgent(n, f, m), why, `${n} ${f} ${m}`);
  }
});

test("New Ship: every advanced option, in one line, every value quoted", async () => {
  const { fleet } = await readFleet();
  const form = { name: "ship-07", signins: null, agent: "lead", folder: "~/work/lead", agent_mode: "acceptEdits", github_scopes: ["write:packages", "workflow"], no_overlay: true, hosts: "ship-07.example.com, 192.0.2.7", vmid: "0120", mode: "plan", allow_bypass: true, agent_sudo: true, timezone: "America/Argentina/Buenos_Aires" };
  const plan = newShipJob(fleet.state, form);
  assert.deepEqual(plan.errors, []);
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' '--autostart' 'lead:~/work/lead:acceptEdits' '--github-scopes' 'workflow,write:packages' '--no-overlay' '--vmid' '120' '--host' 'ship-07.example.com' '--host' '192.0.2.7' '--mode' 'plan' '--allow-bypass' '--agent-sudo' '--timezone' 'America/Argentina/Buenos_Aires' && 'ship-fleet' 'login' 'ship-07' 'claude'");
  assert.deepEqual(shWords(plan.job.shown), plan.job.argv);
  // A folder with a space still runs as one word, on both shells.
  const spaced = newShipJob(fleet.state, { name: "ship-07", signins: null, folder: "~/my work/it's" });
  assert.deepEqual(shWords(spaced.job.shown), spaced.job.argv);
  assert.deepEqual(shWords(spaced.job.remote.split(" && ")[0]), ["ship-fleet", "new", "ship-07", "--autostart", "main:~/my work/it's"]);
});

test("New Ship: never the first prompt, and no flag of its own that skips a question", async () => {
  const { fleet } = await readFleet();
  const plan = newShipJob(fleet.state, { name: "ship-07", signins: null, prompt: "Get going.", autostart_prompt: "Get going.", yes: true, no_wait: true, force: true });
  assert.equal(plan.job.remote, "'ship-fleet' 'new' 'ship-07' '--autostart' 'main:~/work' && 'ship-fleet' 'login' 'ship-07' 'claude'");
});

test("New Ship: advanced values ship-fleet would refuse are said, and nothing is built", async () => {
  const { fleet } = await readFleet();
  const bad = newShipJob(fleet.state, { name: "ship-07", signins: null, agent: "Lead", github_scopes: ["admin:org"], hosts: "ok.example.com -bad", vmid: "99", mode: "yolo", timezone: "europe/berlin" });
  assert.equal(bad.job, undefined);
  assert.equal(bad.errors.length, 6);
  assert.match(bad.errors.join("\n"), /not an agent name[\s\S]*GitHub scope FleetLedger offers: admin:org[\s\S]*VM id[\s\S]*"-bad" is not a host[\s\S]*not a mode[\s\S]*not a time zone/);
  assert.equal(checkVmid(""), null);
  assert.equal(checkVmid("999999999"), "999999999");
  for (const v of ["99", "1000000000", "1e3", "-100", "12a", "1 2"]) assert.throws(() => checkVmid(v), /VM id/, v);
  assert.equal(checkHost(" ship-07.example.com "), "ship-07.example.com");
  for (const h of ["-a", ".a", "a_b", "a b", "a/b", "x".repeat(254), ""]) assert.throws(() => checkHost(h), /not a host/, h);
  // A first agent in bypassPermissions: only where the Ship allows it (ship.toml's own rule).
  const bypass = { name: "ship-07", signins: null, agent_mode: "bypassPermissions" };
  assert.match(newShipJob(fleet.state, bypass).errors.join(" "), /needs allow bypassPermissions/);
  assert.match(newShipJob(fleet.state, { ...bypass, allow_bypass: true }).job.remote, /'--autostart' 'main:~\/work:bypassPermissions' .*'--allow-bypass'/);
  assert.match(newShipJob(fleet.state, { ...bypass, mode: "bypassPermissions" }).job.remote, /'--mode' 'bypassPermissions'/);
  assert.equal(checkTimezone(""), null);
  for (const z of ["UTC", "Etc/GMT+5", "America/Indiana/Knox"]) assert.equal(checkTimezone(z), z);
  for (const z of ["utc", "Europe/", "A/B/C/D", "Europe/Ber lin", "../etc"]) assert.throws(() => checkTimezone(z), /not a time zone/, z);
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

test("New Ship: driver, image, tools and profiles are read when it opens, three reads at once", async () => {
  const s = await readFleet();
  assert.equal(s.fleet.state.choices, null);
  const before = s.calls.length;
  const reading = s.fleet.loadChoices();
  assert.deepEqual(s.fleet.state.choices, { loading: true });
  await reading;
  const lines = s.calls.slice(before).map((c) => c.args.at(-1));
  assert.deepEqual(lines, ["'ship-fleet' 'tools' 'ls' '--json'", "'ship-fleet' 'image' 'ls' '--json'", "'ship-fleet' 'defaults' '--json'"]);
  for (const c of s.calls.slice(before)) assert.ok(c.args.includes("BatchMode=yes")); // reads ask nothing
  const ch = newShipChoices(s.fleet.state);
  assert.deepEqual(ch.why, { tools: null, images: null, defaults: null });
  assert.deepEqual(ch.images.drivers.map((d) => d.id), ["manual", "proxmox"]);
  assert.deepEqual(imagesOn(ch, ""), ["stock"]); // FleetLedger's default driver: manual
  assert.deepEqual(imagesOn(ch, "proxmox"), ["stock", IMG]);
});

test("New Ship: driver and base image, only ones FleetLedger offers there", async () => {
  const { fleet } = await readFleet();
  await fleet.loadChoices();
  const plan = (form) => newShipJob(fleet.state, { name: "ship-07", signins: null, agent_on: false, ...form });
  assert.equal(plan({}).job.remote, "'ship-fleet' 'new' 'ship-07' && 'ship-fleet' 'login' 'ship-07' 'claude'");
  assert.equal(plan({ driver: "proxmox", image: IMG }).job.remote, `'ship-fleet' 'new' 'ship-07' '--driver' 'proxmox' '--image' '${IMG}' && 'ship-fleet' 'login' 'ship-07' 'claude'`);
  assert.equal(plan({ driver: "proxmox", image: "stock" }).job.remote, "'ship-fleet' 'new' 'ship-07' '--driver' 'proxmox' '--image' 'stock' && 'ship-fleet' 'login' 'ship-07' 'claude'");
  assert.match(plan({ image: IMG }).errors.join(" "), /is not one published to the manual driver/);
  assert.match(plan({ driver: "cloud" }).errors.join(" "), /"cloud" is not a driver set up on FleetLedger/);
  // proxmox not set up: only manual is offered.
  const manual = await readFleet({ answers: { image: ok(example("fleetledger-images", "stock-only.json")) } });
  await manual.fleet.loadChoices();
  assert.match(newShipJob(manual.fleet.state, { name: "ship-07", driver: "proxmox" }).errors.join(" "), /not a driver set up/);
  // A pinned image the driver cannot start from: `new` would refuse it, so the form says so.
  const pinned = await readFleet({ answers: { image: ok(example("fleetledger-images", "pinned-on-proxmox.json")) } });
  await pinned.fleet.loadChoices();
  const p = (form) => newShipJob(pinned.fleet.state, { name: "ship-07", signins: null, agent_on: false, ...form });
  assert.match(p({}).errors.join(" "), /The pinned image 26\.04-20261102-3f9a1c2e is not published to the manual driver/);
  assert.equal(p({ image: "stock" }).errors.length, 0);
  assert.equal(p({ driver: "proxmox" }).job.remote, "'ship-fleet' 'new' 'ship-07' '--driver' 'proxmox' && 'ship-fleet' 'login' 'ship-07' 'claude'");
});

test("New Ship: tools tick from FleetLedger's defaults; --tools adds, --no-default-tools starts from none", async () => {
  const { fleet } = await readFleet();
  await Promise.all([fleet.loadAccounts(), fleet.loadChoices()]);
  const remote = (tools, form = {}) => newShipJob(fleet.state, { name: "ship-07", signins: ["github"], agent_on: false, tools, ...form }).job?.remote;
  const base = "'ship-fleet' 'new' 'ship-07' '--signins' 'github'";
  assert.equal(remote(null), base);
  assert.equal(remote(["ship-axi", "gh-axi"]), base); // exactly the defaults
  assert.equal(remote(["gh-axi", "ship-axi", "tasks-axi"]), `${base} '--tools' 'tasks-axi'`);
  assert.equal(remote(["tasks-axi", "gh-axi"]), `${base} '--no-default-tools' '--tools' 'gh-axi,tasks-axi'`);
  assert.equal(remote([]), `${base} '--no-default-tools'`);
  const bad = newShipJob(fleet.state, { name: "ship-07", tools: ["gh-axi", "ship-axi", "nope"] });
  assert.match(bad.errors.join(" "), /Not a tool FleetLedger offers: nope/);
  // A toolkit's tool goes only to Ships with one of its only_tags (no tag: defaults.tags, work).
  const kit = ["gh-axi", "ship-axi", "examplekit/example-cli"];
  assert.equal(remote(kit), `${base} '--tools' 'examplekit/example-cli'`);
  assert.equal(remote(kit, { tags: ["work", "lab"] }), `'ship-fleet' 'new' 'ship-07' '--tag' 'work' '--tag' 'lab' '--signins' 'github' '--tools' 'examplekit/example-cli'`);
  const elsewhere = newShipJob(fleet.state, { name: "ship-07", tags: ["personal"], tools: kit });
  assert.match(elsewhere.errors.join(" "), /examplekit\/example-cli goes only to Ships tagged work/);
});

test("New Ship: profiles replace FleetLedger's default ones", async () => {
  const { fleet } = await readFleet();
  await Promise.all([fleet.loadAccounts(), fleet.loadChoices()]);
  const plan = (profiles, form = {}) => newShipJob(fleet.state, { name: "ship-07", signins: ["github"], agent_on: false, profiles, ...form });
  assert.equal(plan(["orchestrator"]).job.remote, "'ship-fleet' 'new' 'ship-07' '--signins' 'github' '--profile' 'orchestrator'");
  assert.equal(plan(["examplekit/dev", "orchestrator"]).job.remote, "'ship-fleet' 'new' 'ship-07' '--signins' 'github' '--profile' 'orchestrator' '--profile' 'examplekit/dev'");
  assert.equal(plan([]).job.remote, "'ship-fleet' 'new' 'ship-07' '--signins' 'github'"); // the default: none
  assert.match(plan(["nope"]).errors.join(" "), /Not a profile FleetLedger offers: nope/);
  assert.match(plan(["examplekit/dev"], { tag: "personal" }).errors.join(" "), /examplekit\/dev goes only to Ships tagged work/);
  // Default profiles cannot be left out: `new --profile` replaces them, and none is no flag.
  const withDefaults = await readFleet({ answers: { defaults: ok(example("fleetledger-defaults", "from-ships-toml.json")) } });
  await Promise.all([withDefaults.fleet.loadAccounts(), withDefaults.fleet.loadChoices()]);
  const p = (profiles) => newShipJob(withDefaults.fleet.state, { name: "ship-07", signins: ["github"], agent_on: false, image: "stock", driver: "manual", tag: "work", profiles });
  assert.match(p([]).errors.join(" "), /cannot leave out the default profiles/);
  assert.equal(p(["orchestrator"]).errors.length, 0);
});

test("New Ship: FleetLedger's defaults allowing bypassPermissions let a first agent start in it", async () => {
  const s = await readFleet({ answers: { defaults: ok({ ...example("fleetledger-defaults", "catalogue.json"), mode: "bypassPermissions", allow_bypass: true }) } });
  const form = { name: "ship-07", signins: null, agent_mode: "bypassPermissions" };
  assert.match(newShipJob(s.fleet.state, form).errors.join(" "), /needs allow bypassPermissions/); // not read yet
  await s.fleet.loadChoices();
  assert.equal(newShipJob(s.fleet.state, form).job.remote, "'ship-fleet' 'new' 'ship-07' '--autostart' 'main:~/work:bypassPermissions' && 'ship-fleet' 'login' 'ship-07' 'claude'");
});

test("New Ship: FleetLedger's defaults its lists do not show are kept, and checked where they go", async () => {
  // A default tool and profile from a toolkit that did not load: not listed, never ticked.
  const defaults = { ...example("fleetledger-defaults", "catalogue.json"), tools: ["gh-axi", "ship-axi", "otherkit/x"], tools_from: "defaults", profiles: ["otherkit/p"] };
  const s = await readFleet({ answers: { defaults: ok(defaults) } });
  await Promise.all([s.fleet.loadAccounts(), s.fleet.loadChoices()]);
  const plan = (form) => newShipJob(s.fleet.state, { name: "ship-07", signins: ["github"], agent_on: false, ...form });
  assert.equal(plan({ tools: ["gh-axi", "ship-axi", "tasks-axi"] }).job.remote, "'ship-fleet' 'new' 'ship-07' '--signins' 'github' '--tools' 'tasks-axi'");
  assert.equal(plan({ profiles: [] }).job.remote, "'ship-fleet' 'new' 'ship-07' '--signins' 'github'");
  // A default toolkit tool left as it is still goes only to Ships with its toolkit's tags.
  const kit = await readFleet({ answers: { defaults: ok({ ...defaults, tools: ["gh-axi", "examplekit/example-cli"], profiles: [] }) } });
  await kit.fleet.loadChoices();
  assert.match(newShipJob(kit.fleet.state, { name: "ship-07", signins: null, tag: "personal" }).errors.join(" "), /examplekit\/example-cli goes only to Ships tagged work/);
  assert.equal(newShipJob(kit.fleet.state, { name: "ship-07", signins: null }).errors.length, 0); // defaults.tags: work
});

test("New Ship: a default driver FleetLedger has not set up is said as that", async () => {
  const s = await readFleet({ answers: { defaults: ok({ ...example("fleetledger-defaults", "catalogue.json"), driver: "proxmox" }), image: ok(example("fleetledger-images", "stock-only.json")) } });
  await s.fleet.loadChoices();
  const plan = (form) => newShipJob(s.fleet.state, { name: "ship-07", signins: null, agent_on: false, ...form });
  assert.deepEqual(plan({}).errors, ["FleetLedger's default driver, proxmox, is not set up there: choose one that is."]);
  assert.match(plan({ image: "stock" }).errors.join(" "), /choose a driver that is set up first/);
  assert.equal(plan({ driver: "manual" }).job.remote, "'ship-fleet' 'new' 'ship-07' '--driver' 'manual' && 'ship-fleet' 'login' 'ship-07' 'claude'");
});

test("New Ship: an older FleetLedger, or a read that fails: no choices, the rest still works, and why is said", async () => {
  const usage = (what) => ({ code: 2, stdout: "", stderr: `usage: ship-fleet\nship-fleet: error: ${what}\n` });
  const s = await readFleet({ answers: { tools: usage("unrecognized arguments: --json"), image: usage("unrecognized arguments: --json"), defaults: usage("argument command: invalid choice: 'defaults'") } });
  await s.fleet.loadChoices();
  const ch = newShipChoices(s.fleet.state);
  assert.equal(ch.tools, null);
  assert.match(ch.why.tools, /ship-fleet tools: this FleetLedger is older than the contract this panel reads; update FleetLedger/);
  assert.match(ch.why.defaults, /ship-fleet defaults: this FleetLedger is older/);
  assert.equal(imagesOn(ch, "proxmox"), null);
  const plain = newShipJob(s.fleet.state, { name: "ship-07", signins: null });
  assert.equal(plain.job.remote, "'ship-fleet' 'new' 'ship-07' '--autostart' 'main:~/work' && 'ship-fleet' 'login' 'ship-07' 'claude'");
  const chosen = newShipJob(s.fleet.state, { name: "ship-07", signins: null, driver: "proxmox", image: "stock", tools: ["gh-axi"], profiles: ["orchestrator"] });
  assert.equal(chosen.job, undefined);
  assert.match(chosen.errors.join("\n"), /Driver: FleetLedger's list is not read[\s\S]*Base image: FleetLedger's lists are not read[\s\S]*Tools: FleetLedger's lists are not read[\s\S]*Profiles: FleetLedger's lists are not read/);
  // One read failing leaves the others' choices.
  const one = await readFleet({ answers: { defaults: { code: 1, stdout: "", stderr: "ship-fleet: FleetLedger's vault is locked\n" } } });
  await one.fleet.loadChoices();
  const c = newShipChoices(one.fleet.state);
  assert.ok(c.images && c.tools && !c.defaults);
  assert.match(c.why.defaults, /vault is locked/);
  assert.equal(newShipJob(one.fleet.state, { name: "ship-07", signins: null, driver: "proxmox", image: IMG }).errors.length, 0);
  assert.match(newShipJob(one.fleet.state, { name: "ship-07", signins: null, tools: ["gh-axi"] }).errors.join(" "), /Tools: FleetLedger's lists are not read \(ship-fleet defaults: ship-fleet: FleetLedger's vault is locked\)/);
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
  // a copy on standby refuses ls (offline too) but answers about: the copy is the reason given
  const standby = await readFleet({ about: { ...example("fleetledger-about", "degraded.json"), copy: "standby" }, answers: { ls: { code: 1, stdout: "", stderr: "This copy is on standby.\n" } } });
  assert.match(actionGate(standby.fleet.state), /acts on no Ship \(standby/);
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

test("readTools, readImages and readDefaults read the contracts' examples, and refuse other shapes", () => {
  const tools = readTools(JSON.stringify(example("fleetledger-tools", "with-toolkits.json")));
  assert.deepEqual(tools.tools.map((t) => [t.id, t.default, t.only_tags]), [["gh-axi", true, null], ["ship-axi", true, null], ["tasks-axi", false, null], ["examplekit/example-cli", false, ["work"]], ["examplekit/example-lint", false, ["work"]]]);
  assert.deepEqual(tools.profiles.map((p) => p.id), ["orchestrator", "examplekit/dev"]);
  assert.deepEqual(tools.broken, ["otherkit"]);
  for (const name of ["catalogue.json", "empty.json"]) assert.equal(readTools(JSON.stringify(example("fleetledger-tools", name))).error, undefined);
  const odd = readTools(JSON.stringify({ ...example("fleetledger-tools", "catalogue.json"), tools: [{ id: "Bad Id" }, { id: "x;id" }, { id: "ok-tool", default: "yes" }] }));
  assert.deepEqual(odd.tools, [{ id: "ok-tool", default: false, toolkit: null, only_tags: null }]);
  assert.match(readTools("{}").error, /older than the contract/);
  assert.match(readTools(JSON.stringify({ schema: "shipshape.fleetledger-tools/2" })).error, /newer than this panel reads/);
  const images = readImages(JSON.stringify(example("fleetledger-images", "pinned-on-proxmox.json")));
  assert.deepEqual(images, { pinned: IMG, drivers: [{ id: "manual", images: ["stock"] }, { id: "proxmox", images: ["stock", IMG] }] });
  const strange = readImages(JSON.stringify({ ...IMAGES, drivers: [{ id: "cloud", images: ["stock"] }, { id: "manual", images: ["stock", "../x"] }] }));
  assert.deepEqual(strange.drivers, [{ id: "manual", images: ["stock"] }]);
  assert.match(readImages("{}").error, /older than the contract/);
  const defaults = readDefaults(JSON.stringify(example("fleetledger-defaults", "catalogue.json")));
  assert.deepEqual(defaults, { driver: "manual", image: "stock", tags: ["work"], size: { cores: 4, memory_mb: 8192, disk_gb: 40 }, mode: "auto", allow_bypass: false, tools: ["gh-axi", "ship-axi"], tools_from: "catalogue", profiles: [], timezone: "Europe/Berlin" });
  assert.equal(readDefaults(JSON.stringify(example("fleetledger-defaults", "from-ships-toml.json"))).tools_from, "defaults");
  for (const bad of [{ mode: "yolo" }, { size: { cores: 0, memory_mb: 8192, disk_gb: 40 } }, { timezone: "../etc" }, { tools: ["a b"] }, { driver: "cloud" }]) {
    assert.match(readDefaults(JSON.stringify({ ...example("fleetledger-defaults", "catalogue.json"), ...bad })).error, /not the contract's/, JSON.stringify(bad));
  }
  assert.match(readDefaults("{}").error, /older than the contract/);
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

test("no source file of the mod holds a flag that skips a question", () => {
  const dir = join(ROOT, "herdr", "mods", "fleet");
  const files = readdirSync(dir).filter((f) => f.endsWith(".js"));
  assert.deepEqual(files.sort(), ["main.js", "model.js", "settings.js"]);
  for (const f of files) {
    const code = readFileSync(join(dir, f), "utf8");
    for (const flag of SKIPPING.filter((x) => x !== "-y")) assert.ok(!code.includes(flag), `${f}: ${flag}`);
    assert.ok(!/["'`]-y["'`]/.test(code), `${f}: -y`);
  }
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
    const advanced = newShipJob(fleet.state, { name: "ship-07", tags: ["work", "lab"], signins: null, agent: "lead", folder: "~/work/lead", agent_mode: "bypassPermissions", github_scopes: ["workflow"], no_overlay: true, hosts: "ship-07.example.com", vmid: "120", mode: "auto", allow_bypass: true, agent_sudo: true, timezone: "UTC" });
    if (advanced.job) built.push(advanced);
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
